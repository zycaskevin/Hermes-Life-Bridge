"""Consume existing host budget enforcement; never allocate money or issue grants.

A verified AF authorization is necessary, not sufficient. The host must attest
that its existing effect boundary atomically reserves/enforces TOTAL chargeable
usage (including retries, auxiliary model calls and native tools) for this exact
submission. This module is only the bridge's mandatory consumer/HTTP guard.
There is deliberately no default verifier, local budget ledger, signer or
production enforcer implementation. API health and request max_tokens are not
substitutes. Model/tool arguments cannot install a trusted port.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .hermes_native_events import digest
from .runtime_adapter import RuntimeBridgeError

REQUIRED_COVERAGE = frozenset({
    'cumulative_input_output_tokens', 'all_chargeable_usd',
    'native_tool_call_count', 'authorized_public_network_egress',
    'atomic_reservation_before_effect', 'hold_unknown_outcomes',
    'runtime_deadline',
})
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_CONTEXT_SCHEMA = 'af.native-effect-budget-context.v1'
_CONTEXT_KEYS = frozenset({'schema', 'socketPath', 'invocationRef', 'quotePolicyRef'})


@dataclass(frozen=True, slots=True)
class VerifiedHostBudget:
    """Private host-port return type, not a DLS receipt or execution permission."""
    binding_digest: str
    reservation_ref: str
    verification_ref: str
    origin: str
    issued_at: datetime
    expires_at: datetime
    coverage: frozenset[str]


@runtime_checkable
class ExistingHostBudgetPort(Protocol):
    def bind_execution_context(self, binding: dict, governed) -> dict:
        """Bind the already-governed invocation to the existing host governor."""
        ...

    async def verify_existing_reservation(self, binding: dict) -> VerifiedHostBudget:
        """Authenticate/revalidate original host enforcement, or raise."""
        ...

    def retire_execution_context(self, binding: dict) -> None:
        """Close only reconciled host correlation; never release held budget."""
        ...


@dataclass
class _BoundSubmission:
    binding: dict
    body_template: dict
    governed: object
    deadline: datetime
    execution_context_ref: str = ''
    body_digest: str = ''
    bind_started: bool = False
    host_bind_attempted: bool = False
    bind_failed: bool = False
    host_retired: bool = False
    prepared: bool = False
    consumed: bool = False


class HostBudgetAdmission:
    """Life-scoped pre-POST guard and AF execution-context port."""

    def __init__(self, *, authority_verifier, budget_port, endpoint: str,
                 tenant_id: str, life_did: str, origin: str, capacity: int = 128):
        from agent_factory.policy.execution import ExecutionBoundaryValidator
        if (not isinstance(budget_port, ExistingHostBudgetPort)
                or not callable(getattr(budget_port, 'bind_execution_context', None))
                or not callable(getattr(budget_port, 'verify_existing_reservation', None))
                or not callable(getattr(budget_port, 'retire_execution_context', None))):
            raise RuntimeBridgeError('existing_hard_budget_port_required')
        if (not isinstance(endpoint, str) or not endpoint
                or not isinstance(tenant_id, str) or not tenant_id
                or not isinstance(life_did, str) or not life_did.startswith('did:')
                or origin not in {'SYNTHETIC_TEST', 'LIVE_RUNTIME_REPORTED'}
                or type(capacity) is not int or not 1 <= capacity <= 1024):
            raise RuntimeBridgeError('host_budget_binding_invalid')
        self._validator = ExecutionBoundaryValidator(
            authority_verifier=authority_verifier,
            require_authority_verification=True,
        )
        self._budget_port = budget_port
        self.endpoint = endpoint.rstrip('/')
        self.tenant_id, self.life_did, self.origin = tenant_id, life_did, origin
        self._capacity, self._bound = capacity, {}
        self._governed_index: dict[str, str] = {}
        self._body_index: dict[str, str] = {}
        self._verification_refs: dict[str, str] = {}
        self._lock = threading.RLock()

    def bind_verified_invocation(self, *, request, governed, model, session_id: str):
        """Verify the real AF authorization BEFORE any budget callback or POST."""
        from agent_factory.contracts import ModelConfig
        from agent_factory.contracts.runtime_invocation import GovernedInvocation
        from agent_factory.egress import projected_execution_payload

        if not isinstance(governed, GovernedInvocation) or not isinstance(model, ModelConfig):
            raise RuntimeBridgeError('af_authorized_invocation_required')
        scoped = governed.projection.project_state.get('dlsBinding')
        expected = {
            'tenantId': self.tenant_id, 'lifeDid': self.life_did,
            'invocationId': request.invocation_id,
            'intentDigest': digest(request.intent),
            'contextBinding': request.intent.get('contextBinding'),
        }
        if isinstance(scoped, dict) and scoped.get('schema') == 'dls.af-reserved-binding.v1':
            expected.update(schema='dls.af-reserved-binding.v1', requestedRuntime='hermes',
                            requestedImplementationId=governed.implementation_id)
            if (request.selection.get('selectionRef') != governed.digest()
                    or request.selection.get('selectedRuntime') != governed.selected_runtime
                    or request.selection.get('implementationId') != governed.implementation_id):
                raise RuntimeBridgeError('host_budget_af_selection_mismatch')
        else:
            expected['selectionDigest'] = digest(request.selection)
        if (request.intent.get('tenantId') != self.tenant_id
                or request.intent.get('lifeDid') != self.life_did
                or governed.intent.life_did != self.life_did
                or governed.selected_runtime != 'hermes'
                or scoped != expected
                or json.loads(governed.intent.constraints_json).get('dlsBinding') != scoped
                or not isinstance(session_id, str) or not session_id):
            raise RuntimeBridgeError('host_budget_scope_mismatch')

        task = governed.intent.task(
            governed.projection,
            tokens=governed.token_budget,
            cost=governed.cost_budget,
        )
        self._validator.validate(
            task, governed.projection, governed.execution_authorization
        )
        constraints = json.loads(governed.intent.constraints_json)
        body = {
            'input': json.dumps(
                projected_execution_payload(task, governed.projection),
                ensure_ascii=False,
            ),
            'instructions': (
                'Execute only the supplied authorized ContextProjection; '
                'do not retrieve additional life or relationship state.'
            ),
            'provider': model.provider,
            'model': model.model,
            'model_options': copy.deepcopy(model.parameters),
            'session_id': session_id,
        }
        governed_digest = governed.digest()
        binding = {
            'tenantId': self.tenant_id,
            'lifeDid': self.life_did,
            'invocationRef': digest(request.invocation_id),
            'afInvocationDigest': governed_digest,
            'dlsIntentDigest': digest(request.intent),
            'dlsSelectionDigest': digest(request.selection),
            'authorizationDigest': digest(governed.execution_authorization.to_dict()),
            'contextProjectionDigest': governed.projection.projection_hash(),
            'modelDigest': digest(model.to_dict()),
            'sessionRef': digest(session_id),
            'endpointRef': digest(self.endpoint),
            'limits': {
                'maxTokens': governed.token_budget,
                'maxCostUsd': str(governed.cost_budget),
                'maxNativeCalls': constraints['maxNativeCalls'],
                'maxRuntimeMs': constraints['maxRuntimeMs'],
            },
            'deadline': governed.intent.deadline.isoformat(),
            'requiredCoverage': sorted(REQUIRED_COVERAGE),
        }
        with self._lock:
            if (session_id in self._bound
                    or governed_digest in self._governed_index
                    or len(self._bound) >= self._capacity):
                raise RuntimeBridgeError('host_budget_session_already_bound_or_full')
            self._bound[session_id] = _BoundSubmission(
                binding,
                body,
                copy.deepcopy(governed),
                governed.intent.deadline,
            )
            self._governed_index[governed_digest] = session_id

    @staticmethod
    def _validate_execution_context(context: object) -> dict:
        if (not isinstance(context, dict)
                or set(context) != _CONTEXT_KEYS
                or context.get('schema') != _CONTEXT_SCHEMA
                or not isinstance(context.get('socketPath'), str)
                or not context['socketPath']
                or len(context['socketPath'].encode()) > 512
                or any(not isinstance(context.get(name), str)
                       or _SHA.fullmatch(context[name]) is None
                       for name in ('invocationRef', 'quotePolicyRef'))):
            raise RuntimeBridgeError('host_budget_execution_context_invalid')
        try:
            encoded = json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError, UnicodeError):
            raise RuntimeBridgeError('host_budget_execution_context_invalid') from None
        if not encoded or len(encoded) > 65536:
            raise RuntimeBridgeError('host_budget_execution_context_invalid')
        return copy.deepcopy(context)

    def bind(self, governed) -> dict:
        """AF execution-context port for one exact governed invocation."""
        from agent_factory.contracts.runtime_invocation import GovernedInvocation

        if not isinstance(governed, GovernedInvocation):
            raise RuntimeBridgeError('af_governed_invocation_required')
        governed_digest = governed.digest()
        with self._lock:
            session_id = self._governed_index.get(governed_digest)
            entry = self._bound.get(session_id) if session_id is not None else None
            if (entry is None or entry.prepared or entry.consumed
                    or entry.bind_started or entry.bind_failed
                    or getattr(entry.governed, 'digest', lambda: None)() != governed_digest):
                raise RuntimeBridgeError('host_budget_submission_not_bound_or_replayed')
            entry.bind_started = True
            entry.host_bind_attempted = True
            binding = copy.deepcopy(entry.binding)

        try:
            context = self._budget_port.bind_execution_context(
                binding, copy.deepcopy(governed)
            )
            context = self._validate_execution_context(context)
            body = copy.deepcopy(entry.body_template)
            body['execution_context'] = copy.deepcopy(context)
            body_digest = digest(body)
            context_ref = digest(context)

            with self._lock:
                current = self._bound.get(session_id)
                if (current is not entry or current.prepared or current.consumed
                        or current.bind_failed or not current.bind_started
                        or self._governed_index.get(governed_digest) != session_id):
                    raise RuntimeBridgeError(
                        'host_budget_submission_not_bound_or_replayed')
                if body_digest in self._body_index:
                    raise RuntimeBridgeError('host_budget_submission_already_bound')
                current.binding['submissionDigest'] = body_digest
                current.body_digest = body_digest
                current.execution_context_ref = context_ref
                current.prepared = True
                current.bind_started = False
                self._body_index[body_digest] = session_id
            return copy.deepcopy(context)
        except Exception:
            retire_binding = copy.deepcopy(binding)
            retire_binding['submissionDigest'] = digest({
                'abandonedBeforeSubmission': governed_digest,
            })
            retired = False
            try:
                self._budget_port.retire_execution_context(retire_binding)
                retired = True
            except Exception:
                retired = False
            with self._lock:
                current = self._bound.get(session_id)
                if current is entry:
                    current.bind_started = False
                    current.bind_failed = True
                    current.host_retired = retired
            raise RuntimeBridgeError('host_budget_context_preparation_failed') from None

    async def prepare_execution_context(self, *, session_id: str) -> dict:
        """Compatibility wrapper; the production AF path calls bind directly."""
        with self._lock:
            entry = self._bound.get(session_id)
            if entry is None:
                raise RuntimeBridgeError('host_budget_submission_not_bound_or_replayed')
            governed = copy.deepcopy(entry.governed)
        return await asyncio.to_thread(self.bind, governed)

    def execution_context_ref(self, *, session_id: str) -> str:
        with self._lock:
            entry = self._bound.get(session_id)
            if (entry is None or not entry.prepared
                    or not entry.execution_context_ref):
                raise RuntimeBridgeError('host_budget_execution_context_unavailable')
            return entry.execution_context_ref

    async def before_submit(self, *, endpoint: str, body: dict) -> None:
        """Revalidate the existing reservation immediately before POST."""
        if endpoint.rstrip('/') != self.endpoint or not isinstance(body, dict):
            raise RuntimeBridgeError('host_budget_submission_mismatch')
        body_digest = digest(body)
        with self._lock:
            session = self._body_index.get(body_digest)
            entry = self._bound.get(session) if session is not None else None
            if (entry is None or not entry.prepared or entry.consumed
                    or body_digest != entry.body_digest):
                raise RuntimeBridgeError('host_budget_submission_not_bound_or_replayed')
            entry.consumed = True

        now = datetime.now(timezone.utc)
        if now >= entry.deadline:
            raise RuntimeBridgeError('host_budget_authorization_expired')
        try:
            proof = await self._budget_port.verify_existing_reservation(
                copy.deepcopy(entry.binding)
            )
        except Exception:
            raise RuntimeBridgeError('host_budget_verification_failed') from None
        if type(proof) is not VerifiedHostBudget:
            raise RuntimeBridgeError('host_budget_verified_evidence_required')
        if (proof.binding_digest != digest(entry.binding)
                or proof.origin != self.origin
                or type(proof.coverage) is not frozenset
                or proof.coverage != REQUIRED_COVERAGE
                or any(not isinstance(ref, str) or _SHA.fullmatch(ref) is None
                       for ref in (
                           proof.binding_digest,
                           proof.reservation_ref,
                           proof.verification_ref,
                       ))):
            raise RuntimeBridgeError('host_budget_evidence_binding_mismatch')
        if any(not isinstance(t, datetime)
               or t.tzinfo is None
               or t.utcoffset() is None
               for t in (proof.issued_at, proof.expires_at)):
            raise RuntimeBridgeError('host_budget_evidence_time_invalid')
        now = datetime.now(timezone.utc)
        if not (proof.issued_at <= now < proof.expires_at <= entry.deadline):
            raise RuntimeBridgeError('host_budget_evidence_expired_or_overbroad')

        with self._lock:
            self._verification_refs[digest(session)] = proof.verification_ref

    def discard_unconsumed(self, *, session_id: str) -> bool:
        """Drop only a pre-POST HLB binding after host state is safely closed."""
        with self._lock:
            entry = self._bound.get(session_id)
            if entry is None or entry.consumed or entry.bind_started:
                return False
            governed_digest = entry.binding.get('afInvocationDigest')
            body_digest = entry.body_digest
            host_bind_attempted = entry.host_bind_attempted
            host_retired = entry.host_retired
            retire_binding = copy.deepcopy(entry.binding)
            if 'submissionDigest' not in retire_binding:
                retire_binding['submissionDigest'] = digest({
                    'abandonedBeforeSubmission': governed_digest,
                })

        if host_bind_attempted and not host_retired:
            try:
                self._budget_port.retire_execution_context(retire_binding)
            except Exception:
                return False
            host_retired = True

        with self._lock:
            current = self._bound.get(session_id)
            if (current is not entry or current.consumed or current.bind_started):
                return False
            if host_bind_attempted and not host_retired:
                return False
            self._bound.pop(session_id, None)
            if isinstance(governed_digest, str):
                if self._governed_index.get(governed_digest) == session_id:
                    self._governed_index.pop(governed_digest, None)
            if body_digest and self._body_index.get(body_digest) == session_id:
                self._body_index.pop(body_digest, None)
            self._verification_refs.pop(digest(session_id), None)
        return True

    def retire(self, *, session_id: str) -> bool:
        """Retire reconciled local correlation without refunding durable holds."""
        with self._lock:
            entry = self._bound.get(session_id)
            if (entry is None or not entry.prepared or not entry.consumed):
                return False
            binding = copy.deepcopy(entry.binding)
            governed_digest = binding.get('afInvocationDigest')
            body_digest = entry.body_digest
        try:
            self._budget_port.retire_execution_context(copy.deepcopy(binding))
        except Exception:
            return False
        with self._lock:
            current = self._bound.get(session_id)
            if current is not entry:
                return False
            self._bound.pop(session_id, None)
            if isinstance(governed_digest, str):
                self._governed_index.pop(governed_digest, None)
            if body_digest:
                self._body_index.pop(body_digest, None)
            self._verification_refs.pop(digest(session_id), None)
        return True

    def diagnostics(self) -> dict:
        with self._lock:
            return {
                'boundCount': len(self._bound),
                'verifiedSubmissionCount': len(self._verification_refs),
                'origin': self.origin,
                'localBudgetLedger': False,
                'releaseAttempted': False,
            }
