ARG PYTHON_IMAGE
ARG TEST_BASE_IMAGE
FROM ${TEST_BASE_IMAGE} AS chain
USER 0:0
WORKDIR /usr/local/lib/python3.12/site-packages
COPY pyproject.toml alembic.ini ./
COPY pyproject.toml /opt/tradeos/pyproject.toml
COPY shared ./shared
COPY connectors ./connectors
COPY shared /opt/tradeos/shared
COPY connectors /opt/tradeos/connectors
COPY domains ./domains
COPY workflows ./workflows
COPY tool_gateway ./tool_gateway
COPY artifact_store ./artifact_store
COPY infra ./infra
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
USER 65534:65534
CMD ["python", "-m", "tests.integration.quote_evidence_linux_support"]

FROM chain AS quotation
USER 0:0
COPY apps ./apps
COPY agent_runtime ./agent_runtime
COPY notification_gateway ./notification_gateway
USER 65534:65534
CMD ["python", "-m", "tests.integration.quotation_runtime_linux_support"]

FROM ${TEST_BASE_IMAGE} AS refreshed
USER 0:0
COPY pyproject.toml /opt/tradeos/pyproject.toml
COPY shared /opt/tradeos/shared
COPY connectors /opt/tradeos/connectors
COPY shared /usr/local/lib/python3.12/site-packages/shared
COPY connectors /usr/local/lib/python3.12/site-packages/connectors
COPY tests /usr/local/lib/python3.12/site-packages/tests
USER 65534:65534
CMD ["python", "-m", "pytest", "-c", "/opt/tradeos/pyproject.toml", "/usr/local/lib/python3.12/site-packages/tests/integration/evidence_parser_linux_cases.py", "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_text_profiles.py", "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_parser_client.py", "-q", "--tb=short", "-p", "no:cacheprovider"]

FROM ${PYTHON_IMAGE} AS parser
WORKDIR /opt/tradeos
COPY pyproject.toml ./
COPY shared ./shared
COPY connectors ./connectors
RUN pip install --no-cache-dir '.[dev]'
COPY tests /usr/local/lib/python3.12/site-packages/tests
ENV PYTHON_DOTENV_DISABLED=1
USER 65534:65534
CMD ["python", "-m", "pytest", "-c", "/opt/tradeos/pyproject.toml", "/usr/local/lib/python3.12/site-packages/tests/integration/evidence_parser_linux_cases.py", "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_text_profiles.py", "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_parser_client.py", "-q", "--tb=short", "-p", "no:cacheprovider"]
