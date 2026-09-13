# Phase B Implementation Plan (Codex, 2026-09-12)

## Analysis (spec reading; ambiguities/contradictions found)

Read all four requested specifications in full, every source/template/static stub, and both existing test files. Also checked the product requirements, repository instructions, dependency manifest, and lockfile.

The implementation baseline is small: `main.py` implements `/healthz` and the Mangum handler; one test covers `/healthz`. Everything else is a stub. `python-multipart` is missing from the dependencies. The DynamoDB stub still says **two-digit** sequences; the frozen specification requires **three digits**.

The plan follows [repository structure](docs/04_repository_structure.md) and [development guidelines](docs/05_development_guidelines.md): typed Python functions, early returns, named constants, AAA tests, mirrored test directories, Japanese UI/documentation, and external-boundary mocks only.

**Specification issues to track explicitly:**

| ID | Finding | Implementation impact |
|---|---|---|
| A1 | Functional design §5.1 specifies a 30-second HTTP timeout; §5.4 specifies two attempts separated by one second, while claiming to fit within a 60-second Lambda timeout. | The nominal budget already exceeds 60 seconds before overhead. An overall deadline or revised timeout needs specification clarification. |
| A2 | §5.4 requires uncorrectable dates/noninteger prices to return a draft with warnings, but the draft contract provides no representation for those invalid values. | Define preservation, replacement, and warning behavior before implementing these cases. Blank date → JST today is already defined. |
| A3 | §3.1/§4.5 use `BatchWriteItem`, but §7 claims the save order prevents database records without images. Batch writes are non-atomic and limited to 25 operations per call; partial writes followed by S3 cleanup can leave records without an image. Best-effort cleanup can also leave images behind. | Support chunking and unprocessed items, but obtain an explicit partial-failure policy. Do not silently substitute transactions or claim atomicity. [AWS BatchWriteItem documentation](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchWriteItem.html) |
| A4 | §4.8 lists unauthenticated CSV responses as 401; §6.2 redirects every unauthenticated non-`/api/*` request to `/login`. §4.2 also omits `/static/*` from its public-route list, although §6.2 and tech spec §6 include it. | Resolve CSV authentication behavior; record the static-route wording inconsistency. The detailed middleware contract explicitly permits static assets. |
| A5 | §3.1 says CSV requires only LineItems, but CSV includes `store`, which exists only on META. §4.7 says excluded is omitted from totals while its response explicitly includes `totals.excluded`. | Confirm META association for CSV and distinguish diagnostic `totals.excluded` from the child/couple dashboard totals. Preserve the explicit response fields while flagging the prose mismatch. |
| A6 | Save accepts `total`, but does not say whether the server trusts it, recomputes it, or rejects mismatches. Month validation specifies a format but not impossible months. “Integer” does not define coercion behavior. | Define these validation cases before locking their expected results into tests. Do not add a mismatch rejection that would contradict nonblocking warnings. |
| A7 | Functional design §5.2 calls for real Gemini prompt tuning during Phase B; tech spec §1 prohibits real APIs in Phase B, and repository structure places the sole allowed smoke script in Phase C. | Keep Phase B tests mocked. Clarify the phase assignment for real-receipt evaluation; do not silently revise the frozen few-shot examples. |
| A8 | The classification guide mentions discount-offset rows under excluded, while rule 2 assigns discounts to the target item’s category. | Clarify that distinction before tuning classifications; retain the supplied guide and few-shot examples without reinterpretation. |
| A9 | Image rejection criteria, configuration variable names for AWS resources, `.env` loading, passphrase character rules/enforcement, CSV ordering, and same-date receipt ordering are not fully defined. | Record these as contract details to settle. Do not invent limits, additional required fields, or undocumented sorting guarantees. |

The following plan can proceed on settled behavior. Tests whose expected outcomes depend on A1–A9 must be finalized after clarification, rather than encoded as guesses.

## Implementation Plan

Each step follows **red → minimal implementation → green → refactor**. Run its targeted acceptance command and, after every step:

```bash
uv run ruff check . && uv run ruff format --check .
```

Paths below are relative to the repository root. Reuse all existing implementation stubs.

1. **Define models and API contracts.**

   **Files:** modify `src/kakeibo/models.py`; create `tests/test_models.py`; extend `tests/conftest.py` with reusable receipt data.

   Separate Gemini input, editable draft, confirmed-save input, stored receipt/items, and summary response models. `uncertain` belongs only to parsed items; explicitly reject it in confirmed items rather than silently ignoring it. Include summary `receipts[].items` with numeric `seq`.

   **Tests first:**
   - `test_save_receipt_with_boundary_item_counts_validates` — parameterize 0, 1, 999, 1000.
   - `test_line_item_with_each_category_validates`
   - `test_line_item_with_negative_price_preserves_amount`
   - `test_line_item_with_unknown_category_fails_validation`
   - `test_save_receipt_with_empty_store_preserves_empty_string`
   - `test_save_receipt_with_uncertain_rejects_draft_field`
   - `test_summary_receipt_with_items_preserves_numeric_sequence`

   Add date, coercion, and saved-total tests after A2/A6 are settled. Do not apply confirmed-save constraints to raw Gemini output indiscriminately.

   **Acceptance:** `uv run pytest -q tests/test_models.py`

2. **Implement configuration and signed-cookie authentication.**

   **Files:** modify `src/kakeibo/config.py`, `src/kakeibo/auth.py`, `tests/conftest.py`; create `tests/test_config.py`, `tests/test_auth.py`.

   Load secrets from configuration. Use `secrets.compare_digest`, `URLSafeTimedSerializer`, a 90-day maximum age, and payload `{"auth": true, "pv": sha256(APP_PASSCODE).hexdigest()[:8]}`. Default `COOKIE_SECURE` to true. Implement middleware policy without database access.

   Keep configuration and client creation testable without opening network connections during imports. Tests use synthetic credentials, including a 12+ character passphrase.

   **Tests first:**
   - `test_config_without_cookie_secure_defaults_to_true`
   - `test_config_with_cookie_secure_false_disables_secure_flag`
   - `test_session_with_valid_signature_and_payload_authenticates`
   - `test_session_with_tampered_expired_or_invalid_payload_is_rejected`
   - `test_session_after_passcode_change_is_rejected`
   - `test_session_after_secret_change_is_rejected`
   - `test_auth_without_session_returns_api_401`
   - `test_auth_without_session_redirects_pages`
   - `test_auth_for_public_paths_allows_access`
   - `test_passcode_after_repeated_failures_still_authenticates`

   Settle A4/A9 before adding CSV-specific and passphrase-policy assertions. Include public-path lookalikes to ensure exemptions match only intended paths.

   **Acceptance:** `uv run pytest -q tests/test_models.py tests/test_config.py tests/test_auth.py`

3. **Implement DynamoDB persistence with moto.**

   **Files:** modify `src/kakeibo/repositories/dynamo.py`, `tests/conftest.py`; create `tests/repositories/test_dynamo.py`.

   Create moto fixtures for the single table. Persist META and `ITEM#001`–`ITEM#999`, including denormalized item dates. Preserve integer amounts and item order. Implement complete Scan/Query pagination, batch chunking, and unprocessed-item handling. Correct the stale stub comment.

   **Tests first:**
   - `test_save_receipt_with_items_writes_exact_entity_attributes`
   - `test_save_receipt_with_999_items_writes_all_entities`
   - `test_query_receipt_with_multiple_pages_returns_all_items_in_order`
   - `test_scan_with_multiple_pages_returns_all_entities`
   - `test_batch_write_with_unprocessed_items_retries_remaining_items`
   - `test_delete_receipt_with_items_removes_only_target_partition`
   - `test_delete_receipt_without_partition_reports_not_found`
   - `test_repository_round_trip_preserves_negative_integer_prices`

   Use moto for normal persistence and AWS-boundary fault injection for partial responses/errors. A3 determines terminal partial-write/delete recovery assertions.

   **Acceptance:** `uv run pytest -q tests/repositories/test_dynamo.py`

4. **Implement S3 image operations with moto.**

   **Files:** modify `src/kakeibo/repositories/s3.py`, `tests/conftest.py`; create `tests/repositories/test_s3.py`.

   Provide image put/get and cleanup delete operations. Keep boto3 exclusively in repositories. The service will supply the confirmed-date key; no image-serving endpoint is introduced.

   **Tests first:**
   - `test_put_image_with_jpeg_preserves_bytes_and_content_type`
   - `test_get_image_with_existing_key_returns_original_bytes`
   - `test_delete_image_with_existing_key_removes_object`
   - `test_put_image_with_aws_failure_propagates_failure`
   - `test_delete_image_with_aws_failure_propagates_failure`

   **Acceptance:** `uv run pytest -q tests/repositories`

5. **Implement Gemini parsing and draft warnings.**

   **Files:** modify `src/kakeibo/services/gemini.py`, `tests/conftest.py`; create `tests/services/test_gemini.py`.

   Use direct httpx REST and `httpx.MockTransport`. Send inline base64 JPEG, the documented classification guide/few-shot examples, and structured-output schema including required `uncertain`. Keep raw-response handling separate from confirmed-save validation.

   **Tests first:**
   - `test_parse_receipt_with_jpeg_sends_documented_rest_request`
   - `test_parse_receipt_with_valid_response_preserves_uncertain`
   - `test_parse_receipt_with_total_mismatch_adds_warning`
   - `test_parse_receipt_with_matching_total_has_no_mismatch_warning`
   - `test_parse_receipt_with_blank_date_uses_jst_today_and_warns`
   - `test_parse_receipt_with_retryable_failure_retries_once`
   - `test_parse_receipt_with_repeated_failure_reports_parse_failure`
   - `test_parse_receipt_with_429_or_other_4xx_does_not_retry`
   - `test_parse_receipt_with_success_does_not_write_storage`

   Parameterize retries over timeout, 5xx, and malformed JSON. Test JST fallback across a UTC/JST date boundary. Verify the fixed retry delay without mocking parsing logic. Finalize deadline and semantically invalid-response cases after A1/A2.

   **Acceptance:** `uv run pytest -q tests/services/test_gemini.py`

6. **Implement confirmed-save and deletion orchestration.**

   **Files:** create `src/kakeibo/services/receipts.py` and `tests/services/test_receipts.py`; extend shared fixtures as needed.

   This small new service is necessary to preserve `routers → services → repositories`; save coordination does not belong in HTTP handlers or the Gemini service.

   Validate before side effects; generate ULID only for confirmation; derive `receipts/YYYY/MM/<id>.jpg` from the confirmed date; create a JST `created_at`; put S3 before DynamoDB. On database failure, attempt S3 cleanup. Deletion removes database entities and retains the image.

   **Tests first:**
   - `test_confirm_receipt_with_corrected_date_uses_confirmed_month_key`
   - `test_confirm_receipt_with_valid_input_generates_ulid_and_jst_timestamp`
   - `test_confirm_receipt_with_success_uploads_image_before_database_write`
   - `test_confirm_receipt_with_invalid_input_has_no_storage_side_effects`
   - `test_confirm_receipt_with_s3_failure_leaves_database_empty`
   - `test_confirm_receipt_with_database_failure_attempts_image_cleanup`
   - `test_confirm_receipt_with_cleanup_failure_preserves_save_failure`
   - `test_delete_receipt_with_existing_image_retains_image`

   Exercise real service/repository code against moto; inject failures at AWS boundaries. Add partial-write recovery tests only after A3 is settled.

   **Acceptance:** `uv run pytest -q tests/services/test_receipts.py tests/repositories`

7. **Implement summary aggregation and CSV data preparation.**

   **Files:** modify `src/kakeibo/services/summary.py`; create `tests/services/test_summary.py`.

   Aggregate scanned items by receipt date, category, and day. Associate META with items for receipt listings and CSV, subject to A5 confirmation. Return receipt items ordered by numeric sequence and receipts by descending purchase date. Keep daily results sparse; the browser performs zero filling.

   Reuse this service to prepare CSV rows; HTTP serialization remains in the export router.

   **Tests first:**
   - `test_summary_with_mixed_months_includes_only_requested_month`
   - `test_summary_with_discounts_preserves_negative_amounts`
   - `test_summary_with_excluded_items_keeps_child_and_couple_totals_separate`
   - `test_summary_with_multiple_receipts_counts_receipts_not_items`
   - `test_summary_with_backdated_receipts_sorts_by_purchase_date`
   - `test_summary_with_items_returns_sequence_ordered_breakdowns`
   - `test_summary_without_receipts_returns_empty_collections_and_zero_totals`
   - `test_export_rows_with_month_or_all_selects_expected_items`
   - `test_export_rows_with_items_includes_receipt_id_store_and_excluded`

   Specify excluded-only days and unresolved ordering details explicitly before adding assertions for them.

   **Acceptance:** `uv run pytest -q tests/services/test_summary.py`

8. **Wire page routes, middleware, and application lifecycle.**

   **Files:** modify `src/kakeibo/main.py`, `src/kakeibo/routers/pages.py`, `tests/conftest.py`, `tests/test_healthz.py`, `pyproject.toml`; regenerate `uv.lock`; create `tests/routers/test_pages.py`, `tests/test_main.py`.

   Add multipart/form support with `uv add python-multipart`. Move `/healthz` into the existing pages router while preserving its response. Wire authentication, Jinja/static handling through `pages.py`, configuration, and reusable clients. Preserve `create_app()` and `kakeibo.main.handler`.

   **Tests first:**
   - `test_healthz_without_authentication_or_external_services_returns_ok`
   - `test_login_with_correct_passcode_sets_cookie_and_redirects`
   - `test_login_with_wrong_passcode_returns_200_with_error_context`
   - `test_login_cookie_has_documented_security_attributes`
   - `test_pages_without_session_redirect_to_login`
   - `test_pages_with_session_return_html`
   - `test_static_without_session_is_accessible`
   - `test_create_app_repeatedly_does_not_duplicate_routes`
   - `test_handler_with_function_url_health_event_returns_ok`

   Check route behavior now using existing template stubs; rendered form/content assertions come in step 10. Use an HTTPS test-client origin when checking secure-cookie behavior.

   **Acceptance:** `uv run pytest -q tests/test_healthz.py tests/test_main.py tests/test_auth.py tests/routers/test_pages.py`

9. **Implement receipt, summary, and CSV HTTP contracts.**

   **Files:** modify `src/kakeibo/routers/receipts.py`, `src/kakeibo/routers/summary.py`, `src/kakeibo/routers/export.py`, `src/kakeibo/main.py`; create corresponding files under `tests/routers/`.

   Routers handle HTTP input/output and map service failures to the documented statuses/details. Parse confirmation’s JSON-valued `payload` form field into the model and return standard 422 details for malformed JSON or invalid fields.

   **Tests first:**
   - `test_parse_endpoint_with_valid_file_returns_draft_without_id_or_s3_key`
   - `test_parse_endpoint_with_missing_or_invalid_file_returns_422`
   - `test_parse_endpoint_with_gemini_failure_returns_documented_502`
   - `test_save_endpoint_with_multipart_payload_and_file_returns_201`
   - `test_save_endpoint_with_invalid_payload_returns_422_without_writes`
   - `test_save_endpoint_with_s3_failure_returns_image_upload_failed`
   - `test_save_endpoint_with_database_failure_returns_save_failed`
   - `test_delete_endpoint_with_existing_receipt_returns_deleted_id`
   - `test_delete_endpoint_with_missing_receipt_returns_404`
   - `test_summary_endpoint_with_valid_month_returns_nested_items`
   - `test_summary_endpoint_with_missing_or_malformed_month_returns_422`
   - `test_export_endpoint_with_month_or_all_returns_csv_attachment`
   - `test_export_endpoint_with_japanese_and_quoted_fields_returns_valid_bom_csv`
   - `test_api_endpoints_without_session_return_401_before_side_effects`

   Assert CSV column order exactly: `receipt_id,date,store,item,price,category`; include negative prices, excluded items, and empty exports. Settle A4 before the unauthenticated CSV test and A9 before image-content validation tests.

   **Acceptance:** `uv run pytest -q tests/routers`

10. **Implement login and upload/confirmation UI.**

    **Files:** modify `templates/base.html`, `templates/login.html`, `templates/upload.html`, `static/app.js`, `static/style.css` under `src/kakeibo/`; extend `tests/routers/test_pages.py`; create `tests/static/test_app.py`; extend `tests/conftest.py`.

    For executable JS behavior tests, add **dev-only** Playwright using `uv add --dev playwright`, then `uv run playwright install webkit`. Use a local test application backed by moto and mocked Gemini HTTP. Keep shared browser/server fixtures in `conftest.py`; no frontend build system is needed.

    **Tests first:**
    - `test_login_page_renders_password_autocomplete_and_japanese_error`
    - `test_upload_with_image_produces_jpeg_with_documented_dimensions_and_quality`
    - `test_upload_with_decode_failure_displays_error_without_request`
    - `test_parse_with_502_opens_manual_draft_and_retains_image`
    - `test_confirmation_with_uncertain_items_shows_count_and_highlights`
    - `test_confirmation_after_category_action_clears_row_uncertainty`
    - `test_confirmation_after_row_edits_updates_all_subtotals`
    - `test_confirmation_on_save_sends_payload_and_same_image_without_uncertain`
    - `test_confirmation_after_save_failure_retains_draft_and_image`
    - `test_confirmation_on_restart_discards_draft_without_writes`
    - `test_confirmation_after_success_resets_upload_view`
    - `test_fetch_with_401_displays_session_message_and_redirects`

    Include blank-row defaults, add/delete behavior, negative amounts, field errors, nonblocking warnings, and JST manual-entry date. Use Japanese labels and category controls at least 44px high.

    **Acceptance:** `uv run pytest -q tests/routers/test_pages.py tests/static/test_app.py`

11. **Implement dashboard UI and complete integration acceptance.**

    **Files:** modify `src/kakeibo/templates/dashboard.html`, `src/kakeibo/static/dashboard.js`, `src/kakeibo/static/style.css`; create `tests/static/test_dashboard.py`; extend receipt-router tests with complete workflows.

    Load Chart.js from a CDN with its major version pinned. Render child/couple cards, daily stacked bars, sparse-day zero filling, month navigation, receipt accordions, deletion, and monthly/all CSV links. Browser tests intercept external CDN traffic deterministically.

    **Tests first:**
    - `test_dashboard_with_sparse_days_zero_fills_selected_month`
    - `test_dashboard_with_excluded_items_omits_excluded_cards_and_series`
    - `test_dashboard_after_month_change_refreshes_summary_and_csv_link`
    - `test_dashboard_with_receipt_expansion_uses_existing_summary_items`
    - `test_dashboard_after_cancelled_delete_sends_no_delete_request`
    - `test_dashboard_after_delete_success_or_404_refreshes_summary`
    - `test_dashboard_with_empty_month_renders_zero_totals`
    - `test_receipt_workflow_parse_confirm_summary_export_delete_is_consistent`
    - `test_receipt_workflow_after_parse_failure_allows_manual_confirmation`

    Cover leap February, year rollover, receipt/item ordering, and session expiry. The workflow tests must verify no persistence after parse, matching S3/CSV IDs after save, and retained S3 images after deletion.

    **Acceptance:**

    ```bash
    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
    uv run pytest --cov=src --cov-report=term-missing
    ```

    Review coverage against the documented approximately 80% target. Complete mobile-width Safari-equivalent manual checks; preserve actual iPhone HEIC/camera acceptance for Phase C as specified.

## Risks

- **Failure consistency:** A3 is the largest correctness risk. Successful moto tests do not establish atomicity across S3 and DynamoDB.
- **Parsing reliability:** Mocked HTTP verifies request/response handling, not OCR accuracy. Real-receipt evaluation remains dependent on A7.
- **Runtime limits:** Retry deadlines and persistent DynamoDB throttling need bounded behavior before deployment.
- **Browser behavior:** Canvas decoding, negative-number entry, and in-memory Blob loss need mobile checks. WebKit automation adds a development-only browser installation.
- **Retrying confirmation:** No idempotency contract exists. A lost successful response followed by resubmission can create duplicates; do not silently add a new API contract.

## Next Steps

Start with models and settled auth/repository behavior. Resolve A1–A9 before implementing their dependent assertions, recording any agreed specification changes in Japanese.

This is an implementation plan only: no files were changed, and acceptance checks were not run. Phase B completion requires the final checks and UI verification above; Phase C remains a separate phase.
