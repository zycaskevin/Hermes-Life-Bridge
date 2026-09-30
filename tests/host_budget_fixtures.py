"""SYNTHETIC budget-verifier metadata, NOT a real reservation/enforcement engine.

Only unit/socket tests import this. No production bootstrap or grant is created.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hermes_life_bridge.host_budget_admission import VerifiedHostBudget, REQUIRED_COVERAGE
from hermes_life_bridge.hermes_native_events import digest


class FixtureHostBudget:
    def __init__(self):
        self.calls = []
        self.retired = []
        self.mode = 'valid'
        self.block = None

    def bind_execution_context(self, binding, governed):
        return {
            'schema': 'af.native-effect-budget-context.v1',
            'socketPath': '/tmp/SYNTHETIC-NOT-A-REAL-BROKER.sock',
            'invocationRef': digest({'fixture': governed.invocation_id}),
            'quotePolicyRef': digest('synthetic-quote-policy'),
        }

    async def prepare_execution_context(self, binding, governed):
        return self.bind_execution_context(binding, governed)

    def retire_execution_context(self, binding):
        self.retired.append(binding.get("afInvocationDigest"))
        return None

    async def verify_existing_reservation(self, binding):
        self.calls.append(binding)
        if self.block is not None:
            await self.block.wait()
        if self.mode == 'deny':
            raise PermissionError('PRIVATE_BUDGET_ERROR')
        if self.mode == 'boolean':
            return True
        now = datetime.now(timezone.utc)
        proof = VerifiedHostBudget(binding_digest=digest(binding),
            reservation_ref=digest('test-only-reservation'),
            verification_ref=digest('test-only-budget-proof'), origin='SYNTHETIC_TEST',
            issued_at=now - timedelta(seconds=1),
            expires_at=datetime.fromisoformat(binding['deadline']), coverage=REQUIRED_COVERAGE)
        if self.mode == 'wrong_binding': return replace(proof, binding_digest=digest('other-life-or-request'))
        if self.mode == 'wrong_origin': return replace(proof, origin='LIVE_RUNTIME_REPORTED')
        if self.mode == 'expired': return replace(proof, expires_at=now-timedelta(seconds=1))
        if self.mode == 'future': return replace(proof, issued_at=now+timedelta(minutes=1))
        if self.mode == 'overbroad_time': return replace(proof, expires_at=proof.expires_at+timedelta(minutes=1))
        if self.mode == 'prompt_only': return replace(proof, coverage=frozenset({'max_tokens_request_parameter'}))
        if self.mode == 'no_cost': return replace(proof, coverage=REQUIRED_COVERAGE-{'all_chargeable_usd'})
        if self.mode == 'no_unknown_hold': return replace(proof, coverage=REQUIRED_COVERAGE-{'hold_unknown_outcomes'})
        if self.mode == 'unbounded_reference': return replace(proof, verification_ref='private://secret')
        return proof
