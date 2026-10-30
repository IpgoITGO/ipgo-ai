FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 ipgo \
    && useradd --system --uid 10001 --gid ipgo --home-dir /app --shell /usr/sbin/nologin ipgo
WORKDIR /app

COPY requirements.txt requirements.txt
COPY requirements requirements
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=ipgo:ipgo app app
COPY --chown=ipgo:ipgo engine engine
COPY --chown=ipgo:ipgo insight insight
COPY --chown=ipgo:ipgo schemas schemas

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

