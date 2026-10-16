# NANCY-FEISHU-RUNTIME-STATUS-001 — 原飛書的多 Runtime 治理狀態入口

日期：2026-10-17（台灣）
狀態：**MODULE DEPLOYED／HLB HEALTHY／OWNER-SCOPE READ-ONLY PROBE PASS**。原飛書新會話由模型自主呼叫仍待真人 E2E。

## 目的

保留 Nancy 原本的飛書聊天入口，以及唯一的 `did:arthurverse:nancy`、Lifetime Hub、DLMF、DLD 和原 Hermes Resident。新增一個只讀的 **`digital_life_runtime_status`**，幫助使用者及 Nancy 區分：

- 既有 Hermes／OpenClaw／Prime 的研究預覽服務是否可連線；
- Nancy Life Runtime 的 restart continuity 是否真正 verified；
- 經 Agent Factory 及 Owner 授權的**正式生產多 Runtime 研究是否可以執行**。

**核心規則：** 預覽健康 `directRuntimes.*=true` 並不代表 AF admission、實際 native run、研究完成或可授權花費。工具的 `productionRuntimeSwitchReady`、`productionGrantVerified` 及 `productionExecutionPermittedByThisTool` 一律 fail-closed 為 false。它不能變更選擇的 Runtime，也不執行任何研究任務。

## 原有流程

```text
原 Nancy 飛書 DM
  → 原 Hermes Gateway
  → 原 HLB Plugin（僅新增 status handler）
  → Hermes state.db 與 Owner allowlisted Feishu DM 源驗證
  → Life Runtime /health（127.0.0.1:8791）
  → DLS Research Preview /health（127.0.0.1:8896）
  → 只輸出安全布林觀測及明確未授權提示
```

兩個 Health URL 全部是程式固定的 loopback，沒有模型可輸入的 URL／端點。HTTP Reader 明確禁用系統 `http_proxy`／`HTTPS_PROXY` 且不跟隨 redirect；限制回應大小及連線逾時。工具參數必須是空物件；錯誤訊息不暴露使用者 ID、Session ID、私密路徑、記憶或金鑰。

## 獨立權限

- 預設 `HLB_RESIDENT_RUNTIME_STATUS_ENABLED=false`：新工具不存在；
- 真實 Nancy DID、既有 `resident-skill-binding.json` 和 `real-conversation-policy.json` 必須通過；
- 只准現有 Owner 綁定的 Feishu 私人 DM 且確有使用者訊息；
- `HLB_AGENT_TOOLS_ENABLED=false` 原封不動，沒有繼承 Lily／Luna 的研究工具權限；
- 對偽造來源、其他 DID、不屬於 Owner 的 Feishu session、非空額外參數都 fail closed；
- 不呼叫 AF RuntimeInvoker、SelfGateway.authorize、LLM、Search、HTTP 研究 API，也不寫 canonical DLMF。

## 真實驗收（2026-10-17 05:53 Asia/Taipei）

- 獨立來源 Git worktree 的完整 HLB 測試執行 **PASS**；Owner 來源、偽造會話、反向代理及 302 redirect 測試 PASS；
- 以 Owner 真實歷史飛書 DM session（僅本機讀取）取得成功的 read-only status 回執：`continuityVerified=true`、`directRuntimeConnectivityObserved.{hermes,openclaw,prime-agent}=true`、`previewResearchAuthorized=false`、`previewLiveExecution=false`、`productionRuntimeSwitchReady=false`；
- 獨立 Codex 安全審查最初指出代理及未驗證飛書 provider 的誤標問題。代理與轉址已修正及複核 PASS；正式安裝回執不再聲稱 Provider E2E；
- 正式單次部署：`NANCY_EXISTING_FEISHU_RUNTIME_STATUS_MODULE_ACTIVE`。既有 Hermes Gateway 重啟成功、飛書 WebSocket 已重新連線；HLB Doctor 五大項全部 HEALTHY；
- 目前裝在原有 HLB 的工具：`digital_life_recall`、`digital_life_runtime_status`、`digital_life_skill_propose`；`digital_life_research` **沒有開啟**；
- 原 HLB env 設定 mode `0600`，備份根目錄 `0700`；
- 備份：`~/.local/state/hermes-life-bridge/resident-recall-backups/runtime-status-20261016T215225661471Z`（只供原機操作者使用，非公開 URL）。

部署只改原 Plugin 的 `config.py`、`plugin.py`、新增 `resident_runtime_status.py` 與一個 opt-in 變數。沒有新 Bot、沒有第二個 Life Runtime、沒有新資料庫。

## 尚未通過的關卡

1. **原飛書真人 E2E**：使用者在 Nancy 既有私聊發送「請用 digital_life_runtime_status 檢查你的 Runtime」，由模型自主呼叫工具；核對 Hermes 真實 `state.db` ToolCall＋ToolResult 和外送文字。現在部署回執明確為 `feishuProviderTurnVerified=false`，不能推測已 PASS。
2. **Agent Factory 正式多 Runtime**：需具有效力的 Owner reconciliation grant／硬預算 Manifest／模型工作階段驗證，並通過 AF pinned Native Implementation、角色隔離與實際執行回執。8896 的 `directRuntimes=true` 只是 Direct Service Health；禁止用模擬 fixture 或 Preview 宣稱 Nancy 能在 Hermes、OpenClaw、Prime 之間正式執行切換。

## 部署入口

```bash
python3 scripts/activate_nancy_resident_runtime_status.py --check
python3 scripts/activate_nancy_resident_runtime_status.py --apply
```

這是一次性版本綁定部署，執行過 `--apply` 後不應再次套用；必須先檢查既有進程／輸出與 repo／service state，避免重跑或回寫其他開發者未提交的修改。需要回滾時，先比對 owner-private 備份與當前版本。
