# Dockerfile
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive

# Instala todas as dependências essenciais de uma vez
RUN apt-get update && \
    apt-get install -y \
    python3 \
    python3-pip \
    iproute2 \
    iputils-ping \
    tcpdump \
    iperf3 && \
    rm -rf /var/lib/apt/lists/*

# Instala bibliotecas Python
RUN pip3 install --no-cache-dir --break-system-packages scapy pythonping netifaces

# Copia nosso script para dentro do contêiner
COPY router.py .

# Define nosso script como o comando a ser executado na inicialização
CMD ["python3", "router.py"]