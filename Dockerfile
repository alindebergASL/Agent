FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=pip_cert \
    if [ -f /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 work-agent \
    && useradd --system --uid 10001 --gid 10001 --no-create-home work-agent \
    && install -d -m 0700 -o work-agent -g work-agent /data

COPY --chmod=u=rwX,go=rX work_agent/ work_agent/
COPY --chmod=u=rwX,go=rX tests/ tests/

USER work-agent
CMD ["uvicorn", "work_agent.web:app", "--host", "0.0.0.0", "--port", "8000"]
