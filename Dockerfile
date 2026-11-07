FROM python:3.15-slim
LABEL org.opencontainers.image.source="https://github.com/jwklijnsma/dahua-onvif" \
      org.opencontainers.image.description="Dahua VTO doorbell as a virtual ONVIF camera"
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dahua_onvif.py .
USER 65534
ENTRYPOINT ["python", "/app/dahua_onvif.py"]
CMD ["/config/config.yaml"]
