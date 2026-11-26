# Security migration — review branch

This branch removes the embedded AI credential and direct provider calls from the Flutter application. A backend proxy is required before testing the AI/OCR features. **No proxy implementation or live deployment is included.** The default branch has not been changed.

## Client configuration

Copy config.example.json to config.local.json and provide the public Supabase URL, public anon/publishable key, and an HTTPS backend URL. Never include provider API keys or a Supabase service-role key in Flutter build configuration: compiled client values are recoverable.

Run flutter run --dart-define-from-file=config.local.json after setting up the proxy. Review compatibility with the installed Flutter SDK.

## Required backend contract

- POST /chat: accept the existing JSON chat payload and return the compatible chat completion response. Restrict models, request size and token usage on the server. Set the provider credential only on the server.
- POST /ocr: accept the existing OCR payload and return the compatible OCR response. Enforce image limits and validate data types; do not retain images by default.
- Both endpoints must validate the Supabase access token from Authorization using a trusted verification mechanism; reject expired or unauthorized sessions. Add per-user quotas, request timeouts and abuse controls. Do not expose a general forwarding endpoint or log sensitive prompts, images or credentials.

## Android signing

Copy android/key.properties.example to android/key.properties and configure the approved signing material outside the repository. Without these settings release signing is not configured; debug use does not need the release key. Do not distribute an unsigned build as a release. The tracked keystore is removed from this branch only. Consult Play App Signing or the existing distribution process before changing keys.

## Before merge

1. Rotate/revoke the exposed provider key with its issuer and review billing. Verify Supabase RLS and key roles.
2. Implement and deploy the authenticated proxy, configure secrets only there, then verify chat, OCR, login and logout on a device.
3. Run flutter analyze, flutter test and the Android debug build; verify signed release separately.
4. Review historical copies and coordinate any history cleanup with collaborators. Removing a key or keystore from the tip does not remove it from old commits.

No live key was tested or revoked; Flutter and Gradle builds have not been run in this review.
