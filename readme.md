# OSPF Gaming - Algoritmo de Roteamento para Jogos Eletrônicos

## Visão Geral

Este projeto implementa uma versão melhorada do protocolo OSPF (Open Shortest Path First), otimizada para redes com tráfego de jogos eletrônicos. A principal inovação é a substituição da métrica de custo padrão do OSPF (baseada apenas na largura de banda) por uma fórmula de custo composta que considera:

- **Largura de Banda (α)**
- **Latência (β)**
- **Perda de Pacotes (γ)**
- **Jitter (δ)**

Essas métricas são cruciais para a qualidade da experiência em jogos online, onde a estabilidade e a rapidez da conexão são mais importantes do que a largura de banda bruta.

## Fórmula de Custo Composta

O coração do projeto é a fórmula de custo ponderada:

`Custo(L) = α⋅(BW_norm) + β⋅(Lat_norm) + γ⋅(Loss_norm) + δ⋅(Jitter_norm)`

Onde:
- **α, β, γ, δ**: Pesos configuráveis que somam 1.0. Para jogos, os pesos de latência, perda de pacotes e jitter são priorizados.
- **_norm**: Valores normalizados das métricas, permitindo a combinação de unidades diferentes em um custo adimensional.

## Topologia da Rede

A topologia da rede é definida em `topologia.mermaid` e implementada em `docker-compose.yml`. Ela consiste em 8 roteadores e 2 hosts, simulando uma rede complexa onde o roteamento otimizado é essencial.
