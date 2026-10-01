"""Concrete DLS NativeRuntimePort using AF's existing Hermes Runs adapter.

No search/read/synthesis implementation, credential loader, default model,
Runtime routing or new authorization issuer lives here. Trusted host composition
supplies existing authority/resolution objects and an admitted Hermes API profile.
The API profile and host must enforce effect-boundary budgets; a prompt or this
client's timeout is not a hard-spend/network sandbox.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from .hermes_native_events import digest, reduce_native_events
from .runtime_adapter import PUBLIC_RESEARCH, RuntimeBridgeError, _contracts


@dataclass
class _Attempt:
    request: Any = field(repr=False)
    task_id: str | None = None
    execution_context_ref: str | None = None
    receipt: Any = field(default=None, repr=False)
    evidence_refs: list[str] = field(default_factory=list)
    finished: bool = False
    confirmed_stopped: bool = False
    answer_consumed: bool = False
    observed_at: float = 0
    phase: str = 'reserved'
    failure_type: str | None = None


class _CaptureTransport:
    """Observe the SAME authenticated event snapshot that AF validates."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.events: dict[str, tuple] = {}
        self.responses: dict[str, Any] = {}

    async def get_json(self, path):
        value = await self.delegate.get_json(path)
        if len(self.responses) >= 8 and path not in self.responses:
            raise RuntimeBridgeError('hermes_control_snapshot_limit')
        self.responses[path] = value
        return value

    async def get_events(self, path):
        value = await self.delegate.get_events(path)
        if path in self.events:
            raise RuntimeBridgeError("hermes_duplicate_event_fetch")
        self.events[path] = value
        return value


class HermesRunsNativePort:
    """One tenant/life, one Hermes execution host, no default activation.

    resolve_governed(NativeInvocation) must return an AF GovernedInvocation
    issued/verified by the EXISTING AF/Self Gateway composition. Its signed
    projection must contain the exact dlsBinding computed by binding_metadata().
    The resolver also owns durable replay reservation and verified host effect
    grants (tool, network, hard token/cost budgets, approved context resolution).
    No test authority is supplied by this package.
    """
    runtime_id = "hermes"

    def __init__(self, *, tenant_id: str, life_did: str, runtime, model,
                 evidence, authority_verifier, resolve_governed: Callable,
                 origin: str, accept_output: Callable | None = None,
                 capacity: int = 128, answer_ttl_seconds: float = 120,
                 submission_gate=None, accept_runtime_receipt: Callable | None = None):
        self.contract, self.projection = _contracts()
        try:
            self.af_native = importlib.import_module('agent_factory.runtimes.hermes_native')
            self.af_api = importlib.import_module('agent_factory.runtimes.hermes_native_api')
            self.af_contract = importlib.import_module('agent_factory.contracts.runtime_invocation')
        except ImportError:
            raise RuntimeBridgeError('af_native_contract_not_installed') from None
        if (not isinstance(tenant_id, str) or not tenant_id
                or not isinstance(life_did, str) or not life_did.startswith('did:')
                or not callable(resolve_governed)
                or origin not in {'SYNTHETIC_TEST', 'LIVE_RUNTIME_REPORTED'}
                or (accept_output is not None and not callable(accept_output))
                or (accept_runtime_receipt is not None and not callable(accept_runtime_receipt))
                or type(capacity) is not int or not 1 <= capacity <= 1024
                or type(answer_ttl_seconds) not in (int, float)
                or not 1 <= answer_ttl_seconds <= 600):
            raise RuntimeBridgeError('hermes_runs_binding_invalid')
        if not isinstance(evidence, self.af_api.HermesApiNativeEvidencePort):
            raise RuntimeBridgeError('authenticated_hermes_api_evidence_required')
        from .host_budget_admission import HostBudgetAdmission
        if not isinstance(submission_gate, HostBudgetAdmission):
            raise RuntimeBridgeError('existing_hard_budget_guard_required')
        # Actual AF constructor enforces the existing strict authority verifier
        # and obtains the canonical Runtime execution context from this gate.
        self._native = self.af_native.HermesNativeCapabilityAdapter(
            runtime=runtime, model=model, evidence=evidence,
            execution_context=submission_gate,
            authority_verifier=authority_verifier)
        self.tenant_id, self.life_did, self.origin = tenant_id, life_did, origin
        self.implementation_id = self.af_api.hermes_native_implementation_id()
        self._runtime, self._model, self._evidence = runtime, model, evidence
        self._authority = authority_verifier
        self._resolve, self._accept_output = resolve_governed, accept_output
        self._accept_runtime_receipt = accept_runtime_receipt
        self._capacity, self._answer_ttl = capacity, float(answer_ttl_seconds)
        self._attempts: dict[str, _Attempt] = {}
        self._loop = None
        self._submission_gate = submission_gate

    def _owner_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._loop is not loop:
            raise RuntimeBridgeError('hermes_host_event_loop_mismatch')

    def _retire_attempt_if_safe(
        self, invocation_id: str, *, now: float | None = None
    ) -> bool:
        """Drop only reconciled local correlation; never release durable budget."""
        entry = self._attempts.get(invocation_id)
        if entry is None or entry.task_id is None:
            return False
        current = time.monotonic() if now is None else now
        if entry.finished:
            expired = (
                entry.observed_at > 0
                and current - entry.observed_at > self._answer_ttl
            )
            if not (entry.answer_consumed or expired):
                return False
        elif not entry.confirmed_stopped:
            return False
        try:
            retired = self._submission_gate.retire(session_id=entry.task_id)
        except Exception:
            return False
        if retired is not True:
            return False
        if self._attempts.get(invocation_id) is entry:
            self._attempts.pop(invocation_id, None)
        return True

    def _sweep_terminal_attempts(self) -> None:
        now = time.monotonic()
        for invocation_id in tuple(self._attempts):
            self._retire_attempt_if_safe(invocation_id, now=now)

    async def discover(self) -> dict:
        self._owner_loop()
        values = await self._native.discover('public_research')
        state = 'unavailable'
        if len(values) == 1:
            d = values[0]
            if d.implementation_id == self.implementation_id and d.availability:
                state = 'available' if (d.health == 'healthy' and d.policy_eligible
                    and d.valid_until > datetime.now(timezone.utc)) else 'unverified'
        return {'schema': self.contract.DESCRIPTOR_SCHEMA, 'runtime': 'hermes',
            'capability': PUBLIC_RESEARCH, 'implementationId': self.implementation_id,
            'nativeTools': {'web_search': ['discover'], 'web_extract': ['read']},
            'requiredEvidenceRoles': ['discover', 'read'], 'availability': state}

    def binding_metadata(self, request) -> dict:
        """Exact metadata for the existing issuer to bind BEFORE signing.

        This helper returns no grant, signature, credential, or policy decision.
        """
        return {'tenantId': self.tenant_id, 'lifeDid': self.life_did,
            'invocationId': request.invocation_id,
            'intentDigest': self.contract.digest(request.intent),
            'selectionDigest': self.contract.digest(request.selection),
            'contextBinding': copy.deepcopy(request.intent['contextBinding'])}

    def _validate_governed(self, request, governed) -> None:
        c = self.af_contract
        if not isinstance(governed, c.GovernedInvocation):
            raise RuntimeBridgeError('af_governed_invocation_required')
        b = request.intent['budget']
        constraints = json.loads(governed.intent.constraints_json)
        expected = self.binding_metadata(request)
        if (governed.invocation_id != request.invocation_id
                or governed.intent.request_id != request.intent['intentId']
                or governed.intent.life_did != self.life_did
                or governed.selected_runtime != 'hermes'
                or governed.implementation_id != self.implementation_id
                or governed.intent.capability != 'public_research'
                or governed.intent.objective != request.intent['request']
                or set(governed.intent.privacy_scope) != {'public_web'}
                or constraints.get('dlsBinding') != expected
                or governed.projection.project_state.get('dlsBinding') != expected
                or governed.projection.project_state.get('capability_intent') != governed.intent.to_dict()):
            raise RuntimeBridgeError('af_dls_exact_binding_mismatch')
        if (governed.token_budget > b['maxTokens']
                or governed.intent.token_budget > b['maxTokens']
                or governed.cost_budget > Decimal(b['maxCostUsd'])
                or governed.intent.cost_budget > Decimal(b['maxCostUsd'])
                or type(constraints.get('maxNativeCalls')) is not int
                or not 1 <= constraints['maxNativeCalls'] <= b['maxNativeCalls']
                or type(constraints.get('maxRuntimeMs')) is not int
                or not 1 <= constraints['maxRuntimeMs'] <= b['maxRuntimeMs']):
            raise RuntimeBridgeError('af_dls_budget_expansion_denied')
        remaining = (governed.intent.deadline - datetime.now(timezone.utc)).total_seconds()
        expires = datetime.fromisoformat(request.selection['expiresAt'].replace('Z', '+00:00'))
        if not 0 < remaining <= b['maxRuntimeMs'] / 1000 or governed.intent.deadline > expires:
            raise RuntimeBridgeError('af_dls_deadline_mismatch')

    async def invoke(self, request, emit) -> dict:
        self._owner_loop()
        if not isinstance(request, self.contract.NativeInvocation):
            raise RuntimeBridgeError('dls_native_invocation_required')
        intent = self.contract.validate_intent(request.intent)
        if intent['tenantId'] != self.tenant_id or intent['lifeDid'] != self.life_did:
            raise RuntimeBridgeError('native_life_scope_mismatch')
        # Retire only terminal/reconciled local correlations before applying
        # the bounded in-memory capacity. Durable budget holds remain in AF.
        self._sweep_terminal_attempts()
        # Every attempt rediscovers actual endpoint capability and readonly policy.
        descriptor = await self.discover()
        self.contract.validate_selection(request.selection, intent, descriptor)
        if descriptor['availability'] != 'available':
            raise RuntimeBridgeError('hermes_native_host_not_ready')
        if request.invocation_id in self._attempts:
            raise RuntimeBridgeError('hermes_attempt_already_reserved')
        # No silent eviction/re-execution. Host lifecycle/reconciliation owns
        # durable attempt records; a full local association table fails closed.
        if len(self._attempts) >= self._capacity:
            raise RuntimeBridgeError('hermes_attempt_capacity')
        entry = _Attempt(copy.deepcopy(request))
        self._attempts[request.invocation_id] = entry
        capture = _CaptureTransport(self._evidence.transport)
        scoped_evidence = self.af_api.HermesApiNativeEvidencePort(
            transport=capture, registry=self._evidence.registry)
        native = self.af_native.HermesNativeCapabilityAdapter(
            runtime=self._runtime, model=self._model, evidence=scoped_evidence,
            execution_context=self._submission_gate,
            authority_verifier=self._authority)
        try:
            entry.phase = 'resolve_governed'
            governed = await self._resolve(copy.deepcopy(request))
            entry.phase = 'validate_binding'
            self._validate_governed(request, governed)
            task = governed.intent.task(governed.projection, tokens=governed.token_budget,
                                        cost=governed.cost_budget)
            # Pure preparation supplies the existing adapter's correlation ID;
            # native.invoke still performs strict external authority verification.
            entry.phase = 'prepare_existing_runtime'
            prepared = self._runtime.prepare_cognitive_task(task, governed.projection,
                governed.execution_authorization, self._model)
            entry.task_id = prepared.runtime_task_id
            entry.phase = 'verify_authority_before_budget_binding'
            await asyncio.to_thread(
                self._submission_gate.bind_verified_invocation,
                request=copy.deepcopy(request), governed=governed, model=self._model,
                session_id=entry.task_id)
            entry.phase = 'invoke_existing_runtime'
            receipt = await native.invoke(governed)
            entry.execution_context_ref = self._submission_gate.execution_context_ref(
                session_id=entry.task_id)
            entry.phase = 'verify_runtime_receipt'
            verifier = self.af_api.HermesApiReceiptVerifier(self._evidence.registry)
            if not verifier.verify(receipt, governed, at=datetime.now(timezone.utc)):
                raise RuntimeBridgeError('hermes_runtime_receipt_not_authenticated')
            if (receipt.invocation_id != request.invocation_id
                    or receipt.invocation_hash != governed.digest()
                    or receipt.life_did != self.life_did or receipt.runtime_id != 'hermes'
                    or receipt.status != 'completed'):
                raise RuntimeBridgeError('hermes_native_receipt_binding_mismatch')
            from urllib.parse import quote
            run_path = '/v1/runs/' + quote(receipt.runtime_run_id, safe='')
            terminal = capture.responses.get(run_path)
            if (terminal is None
                    or terminal.payload.get('session_id') != entry.task_id
                    or terminal.payload.get('execution_context_ref') != entry.execution_context_ref):
                raise RuntimeBridgeError('hermes_native_execution_context_binding_mismatch')
            path = run_path + '/events'
            events = capture.events.get(path)
            entry.phase = 'normalize_native_events'
            if events is None:
                raise RuntimeBridgeError('hermes_native_events_missing')
            callbacks = []
            reduce_native_events(events, run_id=receipt.runtime_run_id,
                invocation_id=request.invocation_id, allowed_tools=set(descriptor['nativeTools']),
                max_calls=intent['budget']['maxNativeCalls'], emit=callbacks.append)
            for event in callbacks:
                emit(event)
            entry.evidence_refs = sorted({ref for e in callbacks for ref in e['evidenceRefs']})
            entry.receipt, entry.finished, entry.confirmed_stopped = receipt, True, True
            entry.observed_at = time.monotonic()
            entry.phase = 'completed'
            if self._accept_runtime_receipt is not None:
                # Hand the authenticated ORIGINAL receipt back to the existing
                # AF broker. Never re-sign, rewrite IDs, or fabricate a receipt.
                await self._accept_runtime_receipt(copy.deepcopy(request), receipt, {
                    'transport': 'hermes-runs',
                    'projectionHash': governed.projection.projection_hash(),
                    'executionContextRef': entry.execution_context_ref,
                    'sessionRef': digest(entry.task_id),
                    'runtimeRunRef': digest(receipt.runtime_run_id),
                    'bindingVerified': True,
                    'rawContextIncluded': False,
                })
            # API evidence does not currently expose provider-reconciled USD.
            # Never relabel a client-side estimate or absent number as actual.
            usage = {'provider': receipt.model_provider, 'model': receipt.model,
                'inputTokens': receipt.input_tokens, 'outputTokens': receipt.output_tokens,
                'totalTokens': receipt.total_tokens}
            return {'invocationId': request.invocation_id, 'runtime': 'hermes',
                'implementationId': self.implementation_id, 'status': 'completed', 'usage': usage}
        except Exception as exc:
            # AF binds the canonical execution context inside native.invoke().
            # If execution then fails/cancels, recover only the already-bound
            # content reference so cancellation can authenticate the terminal
            # run. This never rebinds, retries, releases, or allocates budget.
            if entry.task_id is not None and entry.execution_context_ref is None:
                try:
                    entry.execution_context_ref = (
                        self._submission_gate.execution_context_ref(
                            session_id=entry.task_id)
                    )
                except Exception:
                    pass
            # Bounded operational phase/class only, no raw exception text.
            entry.failure_type = ('bridge_contract' if isinstance(exc, RuntimeBridgeError)
                else 'authority_denied' if isinstance(exc, PermissionError)
                else 'contract_value' if isinstance(exc, ValueError)
                else 'runtime_error')
            if entry.phase in {'resolve_governed', 'validate_binding', 'prepare_existing_runtime',
                    'verify_authority_before_budget_binding'}:
                self._attempts.pop(request.invocation_id, None)
            elif entry.phase == 'invoke_existing_runtime' and entry.task_id is not None:
                try:
                    discarded = self._submission_gate.discard_unconsumed(
                        session_id=entry.task_id)
                except Exception:
                    discarded = False
                if discarded:
                    self._attempts.pop(request.invocation_id, None)
            raise
        finally:
            # The event snapshot may contain runtime text; keep no raw event cache.
            capture.events.clear()
            capture.responses.clear()

    def diagnostics(self) -> dict:
        """Content-free local diagnostics, never an execution authority."""
        return {'runtime': 'hermes', 'implementationId': self.implementation_id,
            'origin': self.origin, 'attemptCount': len(self._attempts),
            'attempts': [{'invocationRef': digest(key), 'phase': item.phase,
                'failureType': item.failure_type, 'finished': item.finished,
                'cancellationConfirmed': item.confirmed_stopped}
                for key, item in self._attempts.items()]}

    async def cancel(self, invocation_id: str) -> bool:
        self._owner_loop()
        entry = self._attempts.get(invocation_id)
        if entry is None:
            return False
        if entry.confirmed_stopped:
            return True
        if entry.task_id is None:
            # No submitted task is known, not proof that other host work stopped.
            return False
        try:
            from urllib.parse import quote
            handle = self._runtime.resume_handle(entry.task_id)
            path = '/v1/runs/' + quote(handle.run_id, safe='')
            async def inspect_status():
                response = await self._evidence.transport.get_json(path)
                body = response.payload
                if (body.get('object') != 'hermes.run' or body.get('run_id') != handle.run_id
                        or body.get('session_id') != handle.runtime_task_id
                        or not entry.execution_context_ref
                        or body.get('execution_context_ref') != entry.execution_context_ref):
                    raise RuntimeBridgeError('hermes_cancel_execution_binding_mismatch')
                return body.get('status')
            async def stop_and_confirm():
                # A cached result can belong to an invalid response. Independently
                # verify exact live run/session binding before stopping or claiming
                # completion; never stop a foreign session returned by mistake.
                status = await inspect_status()
                if status in {'completed', 'failed', 'cancelled'}:
                    return True
                if status not in {'queued', 'started', 'running', 'stopping'}:
                    return False
                await self._runtime.cancel(entry.task_id)
                while True:
                    if await inspect_status() in {'completed', 'failed', 'cancelled'}:
                        return True
                    await asyncio.sleep(0.05)
            entry.confirmed_stopped = await asyncio.wait_for(stop_and_confirm(), timeout=1.0)
        except Exception:
            return False
        if entry.confirmed_stopped:
            self._retire_attempt_if_safe(invocation_id)
        return entry.confirmed_stopped

    async def read_answer(self, receipt: dict) -> dict:
        self._owner_loop()
        if not isinstance(receipt, dict):
            raise RuntimeBridgeError('native_answer_binding_mismatch')
        entry = self._attempts.get(receipt.get('invocationId'))
        if (entry is None or not entry.finished or entry.receipt is None
                or entry.answer_consumed or self._accept_output is None):
            raise RuntimeBridgeError('native_answer_unavailable')
        if time.monotonic() - entry.observed_at > self._answer_ttl:
            entry.receipt = None
            self._retire_attempt_if_safe(receipt.get('invocationId'))
            raise RuntimeBridgeError('native_answer_expired')
        descriptor = {'schema': self.contract.DESCRIPTOR_SCHEMA, 'runtime': 'hermes',
            'capability': PUBLIC_RESEARCH, 'implementationId': self.implementation_id,
            'nativeTools': {'web_search': ['discover'], 'web_extract': ['read']},
            'requiredEvidenceRoles': ['discover', 'read'], 'availability': 'available'}
        checked = self.projection.validate_receipt(receipt, descriptor,
            tenant_id=self.tenant_id, life_did=self.life_did)
        if (checked['status'] != 'completed' or checked['origin'] != self.origin
                or checked['intentDigest'] != self.contract.digest(entry.request.intent)
                or checked['selectionRef'] != entry.request.selection['selectionRef']
                or checked['evidenceRefs'] != entry.evidence_refs):
            raise RuntimeBridgeError('native_answer_binding_mismatch')
        output = entry.receipt.output
        if not isinstance(output, str) or not output.strip() or len(output) > 24000:
            raise RuntimeBridgeError('native_answer_invalid')
        # Consume before awaiting the external acceptance callback; concurrent
        # readers cannot replay the private owner response or race its validator.
        entry.answer_consumed = True
        entry.receipt = None
        try:
            accepted = await self._accept_output(
                copy.deepcopy(entry.request), output, copy.deepcopy(checked))
        finally:
            self._retire_attempt_if_safe(receipt.get('invocationId'))
        if type(accepted) is not bool:
            raise RuntimeBridgeError('native_answer_acceptance_invalid')
        return {'invocationId': checked['invocationId'],
            'receiptDigest': self.contract.digest(checked), 'text': output, 'taskAccepted': accepted}
