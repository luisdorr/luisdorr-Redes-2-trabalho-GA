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
RUN pip3 install --no-cache-dir --break-system-packages scapy pythonping netifaces pyroute2

# Define o diretório de trabalho dentro do contêiner
WORKDIR /opt/ospf-gaming

# Copia o daemon e módulos auxiliares para o contêiner
COPY ospf_gaming_daemon.py algorithm.py metrics.py route_manager.py ./
COPY config/ ./config/

# Define comando a ser executado na inicialização
CMD ["python3", "ospf_gaming_daemon.py", "--config", "/opt/ospf-gaming/config/config.json"]
