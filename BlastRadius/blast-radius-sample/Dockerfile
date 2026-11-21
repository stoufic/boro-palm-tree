FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS map
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage==7.10.7
RUN git clone --filter=blob:none https://github.com/tobymao/sqlglot.git /opt/sqlglot && \
    git -C /opt/sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa && rm -rf /opt/sqlglot/.git
COPY map_plugin.py /app/map_plugin.py
COPY build_map.py /app/build_map.py
WORKDIR /opt/sqlglot
ENV PYTHONPATH=/app:/opt/sqlglot SKIP_INTEGRATION=0
RUN coverage run --data-file=/tmp/.coverage --branch -m pytest -q -p map_plugin; python /app/build_map.py

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py /app/selector.py
COPY --from=map /app/coverage-map.json /app/coverage-map.json
RUN chmod 644 /app/selector.py /app/coverage-map.json
CMD ["python", "/app/selector.py"]
