# PostgreSQL to Oracle Migration Mapping

## Scope
- Source: current PostgreSQL schema used by `loan_engine_v2`
- Target: Oracle schema from `oracle/V1__init_schema.sql`

## Core Type Mapping
- `uuid` / string IDs -> `VARCHAR2(36)`
- `varchar(n)` -> `VARCHAR2(n)`
- `text` -> `CLOB`
- `float` / `double precision` -> `NUMBER(18,2)` for monetary fields, `NUMBER` otherwise
- `integer` -> `NUMBER(10)`
- `boolean` -> `NUMBER(1)` (`1=true`, `0=false`, `NULL=unknown`)
- `timestamp with time zone` -> `TIMESTAMP WITH TIME ZONE`
- `json/jsonb` -> `CLOB` (optionally `IS JSON` constraints in Oracle 21c+)

## Enum Mapping Strategy
PostgreSQL enums are represented as `VARCHAR2` with `CHECK` constraints:
- `gender_type`: `male`, `female`, `other`
- `premises_type`: `Owned`, `Rented`, `Partially Owned`
- `business_nature_type`: `Retailer`, `Wholesaler`, `Wholesaler & Retailer`, `Manufacturer`, `Service Provider`, `Trader`
- `loan_type`: `Unsecured Term Loan`, `Secured Term Loan`
- `application_status`: `PROFILE_SAVED`, `CIBIL_CONSENT_GIVEN`, `CIBIL_OTP_SENT`, `CIBIL_FETCHED`, `AA_CONSENT_GIVEN`, `AA_INIT_DONE`, `AA_FETCHED`, `PROCESSING`, `COMPLETED`, `FAILED`
- `risk_band_type`: `Low Risk`, `Medium Risk`, `High Risk`
- `api_service_type`: `CIBIL`, `AA_INIT`, `AA_FETCH`, `GST_VERIFY`, `MCA_GSTIN_TO_CIN`, `CLAUDE`, `CLAUDE_SUMMARY`
- `credit_category_type`: `Revenue`, `Loan Inward`, `Own Transfer`, `Cash Deposit`
- `txn_type`: `CREDIT`, `DEBIT`

## Boolean Column Mapping
- `has_current_account`, `success`, `eligible`, `is_emi_obligation`, `emi_od_settled`
  - PostgreSQL `true/false` -> Oracle `1/0`
  - Preserve nullability where source allows unknown state.

## JSON Column Mapping
JSON-like fields migrated to `CLOB`:
- `loan_applications`: `cibil_summary`, `bank_metrics`, `engine_output`, `hard_stop_detail`, `claude_summary`
- `api_call_logs`: `request_body`, `response_body`
- `audit_logs`: `extra_metadata`
- `lender_decisions`: `rule_details`

Recommended for Oracle 21c+:
- Add `IS JSON` constraints once initial load succeeds.

## Foreign Keys and Uniqueness
- Keep FK graph unchanged:
  - `signups.borrower_id` -> `borrowers.id` (with `ON DELETE CASCADE`)
  - application/log/decision/label tables -> `loan_applications.id`
- Preserve unique keys:
  - `signups.gstin`
  - `signups.pan`
  - `signups.borrower_id` (1:1 relation with borrower once linked)

## Data Load Order
1. `borrowers`
2. `signups`
3. `loan_applications`
4. `api_call_logs`
5. `audit_logs`
6. `lender_decisions`
7. `transaction_labels`

## ETL Conversion Notes
- Convert booleans during extract:
  - `true -> 1`, `false -> 0`
- Ensure timestamp strings are timezone-aware before insert.
- For CLOB JSON columns, insert serialized JSON text exactly as stored.
- Normalize empty strings for enum-constrained columns:
  - convert `''` to `NULL` to avoid check-constraint failures.

## Validation Checklist Post-Migration
- Row counts match table-by-table.
- FK integrity check passes (no orphan records).
- Unique constraints on `signups` are valid.
- Spot-check workflow records:
  - one borrower, one signup, one application, linked logs.
- Application smoke test:
  - `/health`
  - `/api/v1/signup/verify-gstin`
  - `/api/v1/loan/applications/start`

## Optional: Oracle JSON Hardening (after successful load)
```sql
ALTER TABLE loan_applications ADD CONSTRAINT ck_apps_cibil_summary_json CHECK (cibil_summary IS JSON) ENABLE;
ALTER TABLE loan_applications ADD CONSTRAINT ck_apps_bank_metrics_json CHECK (bank_metrics IS JSON) ENABLE;
ALTER TABLE loan_applications ADD CONSTRAINT ck_apps_engine_output_json CHECK (engine_output IS JSON) ENABLE;
ALTER TABLE api_call_logs   ADD CONSTRAINT ck_api_logs_req_json CHECK (request_body IS JSON) ENABLE;
ALTER TABLE api_call_logs   ADD CONSTRAINT ck_api_logs_resp_json CHECK (response_body IS JSON) ENABLE;
ALTER TABLE audit_logs      ADD CONSTRAINT ck_audit_logs_meta_json CHECK (extra_metadata IS JSON) ENABLE;
ALTER TABLE lender_decisions ADD CONSTRAINT ck_lender_rule_details_json CHECK (rule_details IS JSON) ENABLE;
```

