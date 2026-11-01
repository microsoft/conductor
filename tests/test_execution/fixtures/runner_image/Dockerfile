ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY sitecustomize.py /opt/realm-fixture/sitecustomize.py
ENV PYTHONPATH=/opt/realm-fixture
USER runner
