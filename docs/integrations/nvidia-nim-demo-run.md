# BitCadence + NVIDIA NIM: gated run (2026-11-23 16:16 UTC)

Output of `python scripts/demo_nim_gated.py`, unedited.

```text
Gateway: http://127.0.0.1:53779 (throwaway database in a temp folder)
NIM: https://integrate.api.nvidia.com/v1  model: nvidia/nemotron-3.5-lightning-30b-a3b

1. ops-dispatcher created job bd3a0b2f-9072-4e90-8a19-98a8cd9e3ce9: status = needs_approval
2. NIM worker polled before approval: jobs run = 0
3. human-approver approved: status = pending
4. NIM worker polled after approval: jobs run = 1 (15.0s including the NIM call)
   final status = completed
   model (from handoff) = nvidia/nemotron-3.5-lightning-30b-a3b  endpoint = nvidia-hosted

NIM answer:
- Verify the AI agent’s valid, time‑scoped credentials and required approvals per policy.  
- Validate the change impact analysis: test results, rollback plan, and effect on critical production data.  
- Confirm the change complies with applicable regulations, audit requirements, and BitCadence governance standards.

5. Audit trail (append-only):
   2026-11-23T16:16:37.244955+00:00  created                 by ops-dispatcher
   2026-11-23T16:16:37.777469+00:00  approved                by human-approver
   2026-11-23T16:16:38.516897+00:00  leased                  by nim-1
   2026-11-23T16:16:52.703244+00:00  status:completed        by nim-1
   2026-11-23T16:16:52.724091+00:00  context_distilled       by system
```
