/* ============================================================
   V1__init_schema.sql
   Oracle baseline schema for loan_engine_v2
   ============================================================ */

CREATE TABLE borrowers (
    id                        VARCHAR2(36) PRIMARY KEY,
    name                      VARCHAR2(200) NOT NULL,
    mobile                    VARCHAR2(15) NOT NULL,
    email                     VARCHAR2(200),
    gender                    VARCHAR2(20),
    age                       NUMBER(10),
    date_of_birth             VARCHAR2(10),
    individual_pan            VARCHAR2(10),
    business_name             VARCHAR2(200),
    business_nature           VARCHAR2(100),
    business_industry         VARCHAR2(100),
    business_product          CLOB,
    business_vintage_months   NUMBER(10),
    commercial_premises       VARCHAR2(30),
    residence_premises        VARCHAR2(30),
    pincode                   VARCHAR2(10),
    whatsapp_number           VARCHAR2(15),
    has_current_account       NUMBER(1),
    cibil_consent             VARCHAR2(1),
    cibil_consent_at          TIMESTAMP WITH TIME ZONE,
    aa_consent                VARCHAR2(1),
    aa_consent_at             TIMESTAMP WITH TIME ZONE,
    aa_bank_mobile            VARCHAR2(15),
    created_at                TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at                TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at                TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_borrowers_gender
      CHECK (gender IN ('male','female','other') OR gender IS NULL),
    CONSTRAINT ck_borrowers_business_nature
      CHECK (business_nature IN (
        'Retailer','Wholesaler','Wholesaler & Retailer','Manufacturer','Service Provider','Trader'
      ) OR business_nature IS NULL),
    CONSTRAINT ck_borrowers_commercial_premises
      CHECK (commercial_premises IN ('Owned','Rented','Partially Owned') OR commercial_premises IS NULL),
    CONSTRAINT ck_borrowers_residence_premises
      CHECK (residence_premises IN ('Owned','Rented','Partially Owned') OR residence_premises IS NULL),
    CONSTRAINT ck_borrowers_has_current_account
      CHECK (has_current_account IN (0,1) OR has_current_account IS NULL),
    CONSTRAINT ck_borrowers_cibil_consent
      CHECK (cibil_consent IN ('Y','N') OR cibil_consent IS NULL),
    CONSTRAINT ck_borrowers_aa_consent
      CHECK (aa_consent IN ('Y','N') OR aa_consent IS NULL)
);

CREATE INDEX ix_borrowers_mobile ON borrowers (mobile);

CREATE TABLE signups (
    id                       VARCHAR2(36) PRIMARY KEY,
    borrower_id              VARCHAR2(36),
    gstin                    VARCHAR2(15) NOT NULL,
    pan                      VARCHAR2(10) NOT NULL,
    cin                      VARCHAR2(30),
    date_of_incorporation    VARCHAR2(10),
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_signups_borrower_id UNIQUE (borrower_id),
    CONSTRAINT uq_signups_gstin UNIQUE (gstin),
    CONSTRAINT uq_signups_pan UNIQUE (pan),
    CONSTRAINT fk_signups_borrower
      FOREIGN KEY (borrower_id) REFERENCES borrowers(id) ON DELETE CASCADE
);

CREATE INDEX ix_signups_borrower_id ON signups (borrower_id);
CREATE INDEX ix_signups_gstin ON signups (gstin);
CREATE INDEX ix_signups_pan ON signups (pan);

CREATE TABLE loan_applications (
    id                       VARCHAR2(36) PRIMARY KEY,
    borrower_id              VARCHAR2(36) NOT NULL,
    loan_type                VARCHAR2(40),
    target_loan_amount       NUMBER(18,2),
    status                   VARCHAR2(30) NOT NULL,
    failure_reason           CLOB,
    hard_stop_code           VARCHAR2(20),
    hard_stop_detail         CLOB,
    cibil_client_id          VARCHAR2(100),
    aa_client_id             VARCHAR2(100),
    cibil_summary            CLOB,
    bank_metrics             CLOB,
    engine_output            CLOB,
    emi_od_settled           NUMBER(1),
    safe_loan_amount         NUMBER(18,2),
    risk_band                VARCHAR2(20),
    claude_summary           CLOB,
    processing_time_ms       NUMBER(18,2),
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_apps_borrower
      FOREIGN KEY (borrower_id) REFERENCES borrowers(id),
    CONSTRAINT ck_apps_loan_type
      CHECK (loan_type IN ('Unsecured Term Loan','Secured Term Loan') OR loan_type IS NULL),
    CONSTRAINT ck_apps_status
      CHECK (status IN (
        'PROFILE_SAVED','CIBIL_CONSENT_GIVEN','CIBIL_OTP_SENT','CIBIL_FETCHED',
        'AA_CONSENT_GIVEN','AA_INIT_DONE','AA_FETCHED','PROCESSING','COMPLETED','FAILED'
      )),
    CONSTRAINT ck_apps_risk_band
      CHECK (risk_band IN ('Low Risk','Medium Risk','High Risk') OR risk_band IS NULL),
    CONSTRAINT ck_apps_emi_od_settled
      CHECK (emi_od_settled IN (0,1) OR emi_od_settled IS NULL)
);

CREATE INDEX ix_applications_borrower ON loan_applications (borrower_id);
CREATE INDEX ix_applications_status ON loan_applications (status);
CREATE INDEX ix_applications_created_at ON loan_applications (created_at);
CREATE INDEX ix_applications_hard_stop ON loan_applications (hard_stop_code);

CREATE TABLE api_call_logs (
    id                       VARCHAR2(36) PRIMARY KEY,
    application_id           VARCHAR2(36) NOT NULL,
    service                  VARCHAR2(30) NOT NULL,
    endpoint                 VARCHAR2(300) NOT NULL,
    method                   VARCHAR2(10) NOT NULL,
    request_body             CLOB,
    response_body            CLOB,
    status_code              NUMBER(10),
    duration_ms              NUMBER(18,2),
    success                  NUMBER(1) DEFAULT 0 NOT NULL,
    error_message            CLOB,
    attempt_number           NUMBER(10) DEFAULT 1 NOT NULL,
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_api_logs_app
      FOREIGN KEY (application_id) REFERENCES loan_applications(id),
    CONSTRAINT ck_api_logs_service
      CHECK (service IN (
        'CIBIL','AA_INIT','AA_FETCH','GST_VERIFY','MCA_GSTIN_TO_CIN','CLAUDE','CLAUDE_SUMMARY'
      )),
    CONSTRAINT ck_api_logs_success
      CHECK (success IN (0,1))
);

CREATE INDEX ix_api_logs_application ON api_call_logs (application_id);
CREATE INDEX ix_api_logs_service ON api_call_logs (service);

CREATE TABLE audit_logs (
    id                       VARCHAR2(36) PRIMARY KEY,
    application_id           VARCHAR2(36) NOT NULL,
    event                    VARCHAR2(100) NOT NULL,
    old_status               VARCHAR2(30),
    new_status               VARCHAR2(30),
    actor                    VARCHAR2(50) DEFAULT 'system' NOT NULL,
    extra_metadata           CLOB,
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_audit_logs_app
      FOREIGN KEY (application_id) REFERENCES loan_applications(id)
);

CREATE INDEX ix_audit_logs_application ON audit_logs (application_id);
CREATE INDEX ix_audit_logs_event ON audit_logs (event);

CREATE TABLE lender_decisions (
    id                       VARCHAR2(36) PRIMARY KEY,
    application_id           VARCHAR2(36) NOT NULL,
    lender_name              VARCHAR2(100) NOT NULL,
    eligible                 NUMBER(1) NOT NULL,
    fail_reason              CLOB,
    rule_details             CLOB,
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_lender_decisions_app
      FOREIGN KEY (application_id) REFERENCES loan_applications(id),
    CONSTRAINT ck_lender_decisions_eligible
      CHECK (eligible IN (0,1))
);

CREATE INDEX ix_lender_decisions_application ON lender_decisions (application_id);
CREATE INDEX ix_lender_decisions_lender ON lender_decisions (lender_name);

CREATE TABLE transaction_labels (
    id                       VARCHAR2(36) PRIMARY KEY,
    application_id           VARCHAR2(36) NOT NULL,
    transaction_id           VARCHAR2(100) NOT NULL,
    amount                   NUMBER(18,2) NOT NULL,
    narration                CLOB NOT NULL,
    txn_type                 VARCHAR2(10) NOT NULL,
    credit_category          VARCHAR2(30),
    is_emi_obligation        NUMBER(1),
    emi_lender               VARCHAR2(100),
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    deleted_at               TIMESTAMP WITH TIME ZONE,
    CONSTRAINT fk_txn_labels_app
      FOREIGN KEY (application_id) REFERENCES loan_applications(id),
    CONSTRAINT ck_txn_labels_txn_type
      CHECK (txn_type IN ('CREDIT','DEBIT')),
    CONSTRAINT ck_txn_labels_credit_cat
      CHECK (credit_category IN ('Revenue','Loan Inward','Own Transfer','Cash Deposit') OR credit_category IS NULL),
    CONSTRAINT ck_txn_labels_is_emi
      CHECK (is_emi_obligation IN (0,1) OR is_emi_obligation IS NULL)
);

CREATE INDEX ix_txn_labels_application ON transaction_labels (application_id);
CREATE INDEX ix_txn_labels_txn_id ON transaction_labels (transaction_id);

