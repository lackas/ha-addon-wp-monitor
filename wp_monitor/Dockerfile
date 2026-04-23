ARG BUILD_FROM
FROM python:3.12-alpine

RUN pip3 install --no-cache-dir \
    requests==2.32.3 \
    Authlib==1.4.1 \
    python-socketio==5.12.1 \
    websocket-client==1.8.0

COPY wp_monitor.py /
COPY PyViCareLive/ /PyViCareLive/

ENTRYPOINT ["python3", "-u", "/wp_monitor.py"]
