# FireGuard API Gateway

The gateway applies account-plan limits before starting an analysis.

| Plan | Analysis access | Limits |
| --- | --- | --- |
| Student | Text only | Institutional email and five reports per calendar month |
| Basic | Text and PDF | One purchased report credit per report |
| Pro | Text and PDF | Active subscription required; chatbot entitlement enabled |
| Enterprise | Text and PDF | Unlimited entitlement and external API feature flag |
| Government | Text and PDF | Unlimited entitlement and chatbot/API feature flags |

Use `GET /account/me` with a bearer token to inspect the current plan and remaining quota. Student registration accepts only configured institutional domains from `INSTITUTIONAL_EMAIL_DOMAINS`; production mailbox OTP/link delivery should be connected before treating that domain check as ownership verification. Payment gateway webhooks and plan provisioning are separate integration points; credits and subscriptions must be provisioned by billing before Basic or Pro requests are accepted.

The gateway returns `403` for unavailable features, `402` when payment or an active subscription is required, and `429` when the Student monthly report limit is reached. Quota reservations are refunded when intake or downstream processing fails before a report is produced.

