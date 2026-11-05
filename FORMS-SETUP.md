# Store audit forms: setup after the security fixes

The two forms now share one script, `audit-common.js`. **Deploy all three files together** (`form1_on_reaching_store.html`, `form2_on_store_sealing.html`, `audit-common.js`), side by side in the same folder.

## 1. Turn on the PIN check in n8n (required)

The PIN is no longer written in the page. Anything in a web page's source is public, so the old PIN gave no protection. Now:

- Auditors type a **6-digit PIN**.
- The form sends it with every photo upload and submission, as the HTTP header `X-Audit-Key`.
- **n8n checks it.**

Until you complete the steps below, the webhooks still accept anyone, just as before.

1. Choose a new 6-digit PIN. **Do not reuse the old one.** It was in the public source code, so treat it as known.
2. In n8n, open the **`form1-photo-upload`** Webhook node:
   - *Authentication* → **Header Auth**.
   - Create a credential with Name `X-Audit-Key` and Value = your PIN.
3. Do the same on the **`form1-submit`** Webhook node, using the same credential.
4. Tell the auditors the new PIN.

If a wrong PIN is entered, n8n answers *401/403*, and the form asks for the PIN again.

**If photo uploads stop working right after step 2**, the browser console will show a "CORS" error. That means your n8n version refuses the custom header. Update n8n, or in both HTML files set `const AUTH_IN_BODY = true;`. The PIN then travels inside the request as a field named `auditKey`. In that case, remove that field in n8n before saving rows, so the PIN doesn't land in your sheet.

## 2. Rotate the two WhatsApp group invite links (recommended)

The `TL_DATABASE` in both forms contains the team groups' invite links, plus team-leader phone numbers. These are visible to anyone who opens the page, so anyone with a link can join a group and read the reports.

- In WhatsApp: group → *Invite via link* → **Reset link**.
- Put the new links into both HTML files.

The phone check on the form is a convenience, not security. The n8n PIN check is what protects the data.

## 3. Check how n8n handles Form 2 (please confirm)

Form 2 posts to the **same webhooks as Form 1** (`form1-photo-upload`, `form1-submit`). This was true before and hasn't changed.

Make sure your n8n workflow separates the two forms:

- **Submissions:** using `formType` — `Form1_OnReachingStore` or `Form2_OnStoreSealing`.
- **Photos:** using `fieldId`.

If you'd rather use separate `form2-*` webhooks, create them in n8n and change the two URLs at the top of Form 2's script.

## 4. What changed in the data n8n receives

| Field | Change |
|---|---|
| `auditId` | **New.** One ID per audit, shared by Form 1 and Form 2, so the two records can be joined. Form 1's WhatsApp summary links to Form 2 with it, and Form 2 pre-fills team and store code from that link. |
| `teamKey` | **New.** The internal key, e.g. `teamA`. |
| `teamLeaderName` | Now the display name, e.g. "Team A", instead of the internal key. If an n8n step filtered on `teamA`, switch it to `teamKey`. |
| `linkedFromForm1` | **New** (Form 2 only). `true` when Form 2 was opened from Form 1's link. |
| Photo upload fields | Also include `teamKey` and `auditId`. Each file's extension now comes from its real image type. |
| Photo file names | Begin with the team name (`TeamA_…`), not the key (`Teama_…`). |

## 5. What the auditor will notice

- **PIN:** it's 6 digits, and a wrong PIN is caught by the server, not the page.
- **Photos:** they unlock only after team leader, matching phone and store code are filled in, so every photo is named and filed correctly.
- **Failed uploads:** they show a **Retry** button. Uploads also retry automatically and time out instead of hanging.
- **Formats:** emails, phone numbers, store code, sealing date and team size are checked before submit.
- **Saving:** "Submitted!" appears **only after n8n confirms the report was saved**. On failure the form stays open with a **Retry** button, and nothing is lost.
- **Copy fallback:** if the summary can't be copied automatically, it's shown on the final screen to copy by hand.

## Re-testing after edits

`form-tests/test-forms.js` replays 32 checks against both forms in a real browser (Playwright). Every webhook call is intercepted, so nothing reaches n8n. The checks cover the PIN, photo races, retries, failed submissions, malicious file names, validation, timezones and mobile width:

```bash
python3 -m http.server 8095 --bind 127.0.0.1 &
node form-tests/test-forms.js
```
