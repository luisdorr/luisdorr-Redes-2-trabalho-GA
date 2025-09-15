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

## Como Executar

### 1. Teste Unitário (Simulado)

Para uma demonstração rápida do cálculo de métricas e do algoritmo de Dijkstra, execute o script de teste:

```bash
python3 test_ospf_gaming.py
```

Este script não requer Docker e mostrará:
- Como diferentes condições de rede afetam a métrica OSPF.
- Como o algoritmo de Dijkstra calcula a melhor rota com base nas novas métricas.
- Uma simulação de como a rede se adapta a mudanças nas condições de um link.

### 2. Simulação Completa com Docker

Para simular a rede completa com múltiplos roteadores se comunicando, use o Docker:

**a. Construir e iniciar os contêineres:**

```bash
docker-compose up --build -d
```

**b. Iniciar o roteador em cada contêiner:**

Você pode iniciar o script `router.py` em cada contêiner de roteador (`r1` a `r8`). Por exemplo, para iniciar em `r1`:

```bash
docker exec -it r1 python3 router.py
```

Repita o comando para os outros roteadores.

**c. Observar a descoberta de vizinhos e o cálculo de rotas:**

A saída de cada roteador mostrará:
- Descoberta de vizinhos através de pacotes Hello.
- Cálculo periódico das métricas de jogo para cada vizinho.
- Atualizações da tabela de roteamento com base no algoritmo de Dijkstra.

## Próximos Passos

- **Implementar LSA Flooding**: Atualmente, as métricas são calculadas, mas não são compartilhadas entre todos os roteadores. A implementação do flooding de LSAs (Link State Advertisements) é necessária para que cada roteador tenha uma visão completa da topologia da rede.
- **Medição de Métricas Reais**: A medição de latência, jitter e perda de pacotes é atualmente simulada. Em um ambiente real, seria necessário usar ferramentas como `ping` e `iperf` para obter medições precisas.
- **Interface de Gerenciamento**: Uma interface web ou CLI para visualizar o status da rede, as métricas em tempo real e as tabelas de roteamento seria uma adição valiosa.
