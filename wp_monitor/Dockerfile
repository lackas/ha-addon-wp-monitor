ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip3 install --no-cache-dir \
    requests==2.32.3 \
    Authlib==1.4.1 \
    python-socketio==5.12.1 \
    websocket-client==1.8.0

COPY wp_monitor.py /
COPY PyViCareLive/ /PyViCareLive/

CMD ["python3", "-u", "/wp_monitor.py"]
