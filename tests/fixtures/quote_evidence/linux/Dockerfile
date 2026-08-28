ARG PYTHON_IMAGE
ARG TEST_BASE_IMAGE
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
