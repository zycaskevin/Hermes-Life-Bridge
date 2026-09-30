"""Trusted host bootstrap for the existing HLB/DLS/AF/Hermes composition.

Call on the host's already-running event loop; no loop, gateway, account, profile,
policy, credential source or service is created implicitly. This API is not a
model tool, environment-driven import hook, or authority issuer.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import BridgeConfig
from .deployment_context import _load as load_runtime_manifest
from .hermes_direct_api import DirectHermesApi
from .hermes_runs_binding import HermesRunsNativePort
from .runtime_adapter import (
    BoundResearchBridge, HermesRuntimeAdapter, RuntimeBridgeError,
    get_research_bridge, register_research_bridge,
)


@dataclass(frozen=True)
class NativeBridgeInstallation:
    bridge: BoundResearchBridge = field(repr=False)
    native: HermesRunsNativePort = field(repr=False)

    def status(self):
        return {'registered': True, 'runtime': 'hermes', 'origin': self.native.origin,
            'implementationId': self.native.implementation_id,
            'budgetGuardBound': True, 'liveBudgetEnforcementVerified': False,
            'liveExecutionVerified': False, 'contactPathChanged': False}


async def bootstrap_hermes_runs(config: BridgeConfig, *, base_url: str, auth_token: str,
        model, admission, authority_verifier, prepare, resolve_governed,
        accept_output, origin: str, budget_port=None) -> NativeBridgeInstallation:
    """Bind an explicitly configured/admitted native API host to this life.

    admission/prepare/resolve_governed are supplied by existing AF/Self Gateway
    composition; they MUST verify actual tool/network/hard-budget grants and
    resolve only approved life context. This function never generates a grant.
    model is the AF-selected ModelConfig, not an HLB preference/fallback.
    API liveness/readonly discovery is not a substitute for those authorities.
    """
    if not isinstance(config, BridgeConfig) or not config.agent_tools_enabled:
        raise RuntimeBridgeError('native_bootstrap_tools_not_granted')
    manifest = load_runtime_manifest(config.life_did, config.deployment_manifest_file)
    if manifest['hermes'].get('toolPolicy') != 'governed-readonly':
        raise RuntimeBridgeError('native_bootstrap_policy_mismatch')
    tenant = manifest['dlmfScope']['tenantId']
    if get_research_bridge(life_did=config.life_did, tenant_id=tenant,
                          manifest_hash=manifest['manifestHash']) is not None:
        raise RuntimeBridgeError('native_binding_already_registered')
    if (not callable(getattr(admission, 'admit', None)) or not callable(prepare)
            or not callable(resolve_governed) or not callable(accept_output)):
        raise RuntimeBridgeError('native_bootstrap_trusted_ports_required')
    try:
        from agent_factory.contracts import ModelConfig
        from agent_factory.runtimes.hermes import HermesAdapter
        from agent_factory.runtimes.hermes_native_api import HermesApiEvidenceRegistry, HermesApiNativeEvidencePort
    except ImportError:
        raise RuntimeBridgeError('af_native_contract_not_installed') from None
    if not isinstance(model, ModelConfig):
        raise RuntimeBridgeError('af_selected_model_required')
    from .host_budget_admission import HostBudgetAdmission
    gate = HostBudgetAdmission(authority_verifier=authority_verifier, budget_port=budget_port,
        endpoint=base_url, tenant_id=tenant, life_did=config.life_did, origin=origin)
    transport = DirectHermesApi(base_url=base_url, auth_token=auth_token, submission_gate=gate)
    runtime = HermesAdapter(base_url=base_url, auth_token=auth_token,
        transport=transport.runtime_transport(), supported_model_providers=(model.provider,),
        supported_models=(model.model,), supported_access_methods=(model.access_method,),
        supported_cognitive_capabilities=('public_research',))
    evidence = HermesApiNativeEvidencePort(transport=transport, registry=HermesApiEvidenceRegistry())
    native = HermesRunsNativePort(tenant_id=tenant, life_did=config.life_did,
        runtime=runtime, model=model, evidence=evidence, authority_verifier=authority_verifier,
        resolve_governed=resolve_governed, origin=origin, accept_output=accept_output,
        submission_gate=gate)
    descriptor = await native.discover()
    if descriptor['availability'] != 'available':
        raise RuntimeBridgeError('hermes_native_host_not_ready')
    adapter = HermesRuntimeAdapter(config, tenant_id=tenant, descriptor=descriptor,
        native=native, admission=admission)
    bridge = BoundResearchBridge(adapter=adapter, manifest_hash=manifest['manifestHash'],
        prepare=prepare, read_answer=native.read_answer, host_loop=asyncio.get_running_loop())
    register_research_bridge(bridge)
    return NativeBridgeInstallation(bridge, native)
