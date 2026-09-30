# H2 Market OpenAPI Specifications

This repository generates **a standalone `openapi.yaml` for each H2 market message** defined in the
[`H2-Market-Specifications/h2-market-message-specifications`](https://github.com/H2-Market-Specifications/h2-market-message-specifications) repository.

## Principles

- The source repository remains the single business source of truth for JSON Schemas, catalog definitions, and examples.
- This repository maintains one OpenAPI template file per market message.
- The generator populates only explicitly designated `x-h2-generator` markers.
- Paths, operations, responses, tags, headers, security schemes, and descriptions originate from the templates.
- Payload schemas and examples are injected from the source repository.
- Generated OpenAPI files intentionally omit `jsonSchemaDialect` so that OpenAPI 3.1 tooling defaults to standard JSON Schema dialects without raising unsupported custom dialect warnings.
- `generate-all` removes generated `openapi.yaml` files under the output root that no longer have a catalog entry, for example after a message was renamed, removed, or moved to a new version.
- `generate-all` also bundles the source repository's semantic validation (`tools/validation`) into `mock_backend/_vendor/h2_message_validation/`, together with a `SOURCE.json` that records the source commit. The mock backend uses only this bundle, so it runs without the source repository, and the bundle always matches the committed specifications because both are written by the same run. Never edit the bundle by hand.
- The source repository's location is configured once, in `config/generator.yml` (`source.repository`, `source.ref`).

## Repository Structure

```text
config/
  generator.yml                     Source repository and generator settings
templates/
  common/
    error_components.yaml           Shared headers, security scheme and responses
  messages/
    <type>/
      v<version>/
        <subtype>/
          openapi.template.yaml     Maintained template for one message
dist/
  messages/
    <type>/
      v<version>/
        <subtype>/
          openapi.yaml              Generated specification for one message (do not edit)
src/
  h2_openapi_generator/             Generator, validator and validator bundling (command line)
mock_backend/
  app.py                            Mock API with Scalar and Swagger UI
  validation/                       Header, schema and semantic validation
  _vendor/
    h2_message_validation/          Generated copy of the source repository's semantic validation (do not edit)
  README.md                         Architecture and validation details of the mock backend
tests/                              Generator and mock backend tests
.github/
  workflows/                        GitHub Actions (see Validation)
requirements-dev.txt                Dependencies for the generator, the mock backend and the tests
```

`<type>` and `<subtype>` are the message type and subtype in lower case; `<version>` is the message format version.

Examples:

```text
templates/messages/measurement/v0.9/preliminary/openapi.template.yaml
templates/messages/measurement/v0.9/final/openapi.template.yaml
```

## Published APIs in This Repository

Each message has its own specification with one `POST` operation:

| Message | Subtype | Endpoint | `H2-Business-Process` | Sender → Recipient |
|---|---|---|---|---|
| `ALLOCATION` | `Measurement` | `/api/v1/measurement-allocations` | `measurementAllocationSubmission` | WMGV → BKV |
| `ALLOCATION` | `Nomination` | `/api/v1/nomination-allocations` | `nominationAllocationSubmission` | WNB, WMGV → WMGV, BKV |
| `ALLOCATION` | `QuantityDeclaration` | `/api/v1/quantitydeclaration-allocations` | `quantitydeclarationAllocationSubmission` | WNB, WMGV → WMGV, BKV |
| `BALANCING` | `ContinuousBalancing` | `/api/v1/continuous-balancings` | `continuousBalancingSubmission` | WMGV → BKV |
| `BALANCING` | `DifferenceQuantities` | `/api/v1/difference-quantities` | `differenceQuantitiesBalancingSubmission` | WMGV → BKV |
| `BALANCINGGROUPLIST` | `Default` | `/api/v1/balancinggrouplists` | `balancingGroupListSubmission` | WMGV → WNB |
| `MATCHING` | `Request` | `/api/v1/matching-requests` | `requestMatchingSubmission` | WNB → WNB |
| `MATCHING` | `Response` | `/api/v1/response-matching` | `responseMatchingSubmission` | WNB → WNB |
| `MEASUREMENT` | `Final` | `/api/v1/final-measurements` | `finalMeasurementSubmission` | WNB → WMGV, TK |
| `MEASUREMENT` | `Preliminary` | `/api/v1/preliminary-measurements` | `preliminaryMeasurementSubmission` | WNB → WMGV |
| `NOMINATION` | `Physical` | `/api/v1/physical-nominations` | `physicalNominationSubmission` | BKV → WNB |
| `NOMINATION` | `VTP` | `/api/v1/vtp-nominations` | `vtpNominationSubmission` | WMGV, BKV → WMGV |
| `NOMINATIONRESPONSE` | `Physical` | `/api/v1/physical-nominationresponses` | `physicalNominationResponseSubmission` | WNB → BKV |
| `NOMINATIONRESPONSE` | `VTP` | `/api/v1/vtp-nominationresponses` | `vtpNominationResponseSubmission` | WMGV → BKV |
| `QUANTITYDECLARATION` | `Default` | `/api/v1/quantitydeclarations` | `quantityDeclarationSubmission` | BKV → WNB |
| `QUANTITYDECLARATIONRESPONSE` | `Default` | `/api/v1/quantitydeclarationresponses` | `quantityDeclarationResponseSubmission` | WNB → BKV |

Sender and recipient are the market roles that the message schema permits; where several are listed, each of them is allowed. WNB = hydrogen grid operator (`HydrogenGridOperator`), WMGV = hydrogen market area manager (`HydrogenMarketAreaManager`), BKV = balancing group responsible party (`BalancingGroupResponsibleParty`), TK = transport customer (`TransportCustomer`).

The specification for each row is `dist/messages/<type>/v0.9/<subtype>/openapi.yaml`. Its `info.description` and operation description explain the business purpose of the message.

## Versioning

- **API version:** `info.version` of each specification (currently `1.0.0`). The major version of the interface is also part of the path (`/api/v1/`).
- **Message format version:** `x-h2-message.messageVersion` (currently `0.9`). It corresponds to `message.version` in the payload and to the schema version in the message repository.

The `v0.9` in the directory names is the message format version, not the API version.

## Use of AI

OpenAI Codex assisted with the creation and maintenance of this repository.

## API Conventions

All specifications share the same technical conventions, defined once in `templates/common/error_components.yaml`.

**Transport and security:** `POST` with a JSON body (`Content-Type: application/json`), secured with mutual TLS (`mtls`).

**Request headers:**

| Header | Required | Format | Purpose |
|---|---|---|---|
| `H2-Transaction-Id` | yes | UUID version 7, lowercase | Unique identifier of this request on the current communication leg; newly generated for every request, including every retry |
| `H2-Initial-Transaction-Id` | only on retries | UUID version 7, lowercase | `H2-Transaction-Id` of the first request of the same communication leg; must not be sent on a request that is not a retry |
| `H2-Message-Sender` | yes | 13 digits | Market partner identifier of the sender |
| `H2-Message-Receiver` | yes | 13 digits | Market partner identifier of the recipient of the current communication leg (for example the Data Hub), not necessarily the final business recipient |
| `H2-Business-Process` | yes | One fixed value per endpoint | Business process of the call (see the table above) |

The **effective transaction ID** of a request is its `H2-Initial-Transaction-Id` on a retry and its `H2-Transaction-Id` otherwise.

**Response headers:** Every response carries `H2-API-Version` with the fully qualified API version (`<MAJOR>.<MINOR>.<PATCH>`, e.g. `1.0.0`). Every response with a JSON body also carries a newly generated `H2-Transaction-Id` (UUID version 7) and `H2-Reference-Id` with the effective transaction ID of the answered request. All responses carry `H2-Response-Origin`; `405` adds `Allow` with the permitted methods, and `429` and `503` add `Retry-After`.

**Success response (`200`):** `application/json` with `status` (`ok`) and `responseOrigin` (`TargetSystem`), and optionally `referenceNumber` (the `documentNumber` of the message), `messageType`, `messageSubType`, `messageVersion` and `processedAt`.

**Error responses:** `application/json` with an `ErrorResponse`: `code` (machine-readable error code), `title` and `transactionId` (the effective transaction ID), and optionally `detail`. `responseOrigin` (`Gateway` or `TargetSystem`) and `errors` (a list of entries with `path` and `message`) extend this format. A request without a syntactically valid `H2-Transaction-Id` is answered without a body and without `H2-Transaction-Id` and `H2-Reference-Id` headers.

**Status codes:** `200`, `400`, `401`, `403`, `404`, `405`, `409`, `415`, `422`, `429`, `502`, `503` and `504`. According to the response descriptions, `409` and `422` come from the target system after the message was forwarded; the other errors come from the gateway.

The mock backend returns only some of these responses; see [mock_backend/README.md](mock_backend/README.md#responses).

## Local Setup & Generation

```bash
python -m pip install -r requirements-dev.txt

git clone https://github.com/H2-Market-Specifications/h2-market-message-specifications.git _source/h2-market-message-specifications

python -m h2_openapi_generator generate-all --source-root _source/h2-market-message-specifications --templates-root templates --output-root dist

# or with relaxed template verification:
python -m h2_openapi_generator generate-all --source-root _source/h2-market-message-specifications --templates-root templates --output-root dist --no-strict-templates

# Validate all generated OpenAPI specifications:
python -m h2_openapi_generator validate-all --output-root dist
```

## Running the Mock API Locally

The mock backend does not need the source repository.

```bash
python -m pip install -r requirements-dev.txt
python -m uvicorn mock_backend.app:app --reload --port 8000
```

Interactive Scalar UI documentation is accessible in your browser at: `http://127.0.0.1:8000/scalar`; Swagger UI is available at `http://127.0.0.1:8000/swagger`.

## Mock Backend Scope

The mock backend provides all 16 generated POST endpoints. It serves the unaltered valid examples from the generated OpenAPI specifications and validates:

- Required H2 request headers and their technical formats (e.g. UUID version 7, 13-digit partner IDs);
- Verification that `H2-Business-Process` matches the addressed endpoint;
- `Content-Type: application/json`, JSON syntax, and the associated message schema;
- Stateless period and time-series semantics across message families containing such structures;
- For balancing messages, additional relational verification of interval continuity and sequence.

During gateway and schema validation, all independent errors are aggregated. If gateway validation fails, schema validation is skipped. Each entry in `errors` contains exactly `path` and `message`.

Schema error messages are formatted into a consistent, human-readable structure. Scalar displays each source with the canonical `TYPE / SubType` pair (e.g. `MEASUREMENT / Final`); heterogeneous title suffixes from `info.title` are avoided as source names.

The mock backend intentionally does not perform business, master-data-dependent, or stateful validation. It does not enforce reference data, status, duplicate, or idempotency rules, does not simulate target backends, and does not compare header partners against partners in the message body. `H2-Business-Process` is treated strictly as a technical gateway header rather than a business validation step. Consequently, repeated identical requests are accepted. For complete architecture and validation details, see [mock_backend/README.md](mock_backend/README.md).

## Generating a Single Message

```bash
python -m h2_openapi_generator generate-message \
  --source-root _source/h2-market-message-specifications \
  --templates-root templates \
  --output-root dist \
  --message-id preliminary-measurement \
  --message-version 0.9
```

## Template Markers

### Payload Schema Injection

```yaml
components:
  schemas:
    preliminaryMeasurement:
      x-h2-generator:
        inject: message-schema
```

The marker is replaced with the bundled JSON Schema of the respective message.

### Catalog Examples Injection

```yaml
examples:
  CatalogExamples:
    x-h2-generator:
      inject: all-valid-examples
```

The marker is replaced with all valid examples referenced in the catalog for that message.

## Validation

Validation runs in GitHub Actions:

- `validate.yml` runs on every push and pull request. It runs the linter (`ruff`) and the tests (`pytest`), fails if the committed specifications or the validator bundle differ from a fresh regeneration against the source repository, and checks that the built Python package contains all specifications and the validator bundle.
- `generate-openapi.yml` checks out the source repository, regenerates all message-specific OpenAPI specifications and the validator bundle, and commits any changes under `dist/messages` and `mock_backend/_vendor` directly to `main`.

Both workflows read the source repository from `config/generator.yml`. While that repository is private, set the `H2_MARKET_MESSAGES_REPO_TOKEN` secret to a token with read access to it; for a public source repository no secret is needed.

The same checks can be run locally, after cloning the source repository as described under Local Setup & Generation:

```bash
python -m pip install -r requirements-dev.txt
ruff check .
python -m pytest -q

# Regenerate and check that nothing under dist/messages or mock_backend/_vendor changed.
# SOURCE.json only records the source commit and may change.
python -m h2_openapi_generator generate-all --source-root _source/h2-market-message-specifications --templates-root templates --output-root dist
git status --porcelain -- dist/messages mock_backend/_vendor
```

## Scope & Boundary

This repository does not define a new business API contract. It binds the business-specified OpenAPI templates to the JSON Schemas and examples defined in the upstream message repository.
