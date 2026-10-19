# Image for one code-execution job (SPEC section 8.3). Pinned versions; no network at run time.
FROM python:3.12.13-slim-bookworm

RUN pip install --no-cache-dir \
        pandas==2.3.3 \
        numpy==2.3.5 \
        matplotlib==3.10.8 \
        openpyxl==3.1.5 \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
    # /data: inputs, written by the service as root; read-only to the job user.
    # /output: owned by the job user; the service mounts a size-limited tmpfs over it.
    && mkdir -p /data /output \
    && chown root:root /data && chmod 0755 /data \
    && chown sandbox:sandbox /output

COPY runner.py /opt/relay/runner.py

ENV MPLCONFIGDIR=/tmp/mpl MPLBACKEND=Agg HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER sandbox
WORKDIR /output
ENTRYPOINT ["python", "-I", "/opt/relay/runner.py"]
