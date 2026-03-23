source ./token.sh

CLAUDE_CODE_ENABLE_TELEMETRY=1 \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000 \
    OTEL_METRIC_EXPORT_INTERVAL=5000 \
    claude --dangerously-skip-permissions