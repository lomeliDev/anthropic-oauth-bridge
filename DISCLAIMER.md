# Disclaimer

This project is an independent, community-maintained proxy. It is not affiliated with or endorsed by Anthropic or AWS.

It authenticates to Anthropic with **API keys** (or to AWS Bedrock with your AWS credentials) and is billed
pay-as-you-go on your account. It does **not** use Claude Pro/Max subscription OAuth tokens: Anthropic's terms
(Feb 2026) prohibit their use in third-party tools, and earlier versions of this repository that did so should
not be used.

You are responsible for complying with the Anthropic Usage Policy and AWS terms, and for protecting the API keys
stored in `.env` / `accounts.json`.
