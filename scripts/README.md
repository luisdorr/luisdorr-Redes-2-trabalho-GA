# Scripts de manipulação de links

Este diretório contém utilitários para injetar falhas e recuperar links dentro do
ambiente Docker do projeto **OSPF-Gaming**. Todos os scripts podem ser executados
a partir do host (utilizando `docker exec` automaticamente) ou de dentro dos
próprios containers. Utilize `local` como nome do roteador quando desejar
executá-los diretamente dentro do container.

## Requisitos gerais

- Docker e Docker Compose instalados no host.
- Containers gerados a partir do `generate_compose.py` (garante o bind mount dos
  scripts em `/opt/ospf-gaming/scripts`).
- Permissões para executar `tc` e `ip` dentro dos containers (já providas pela
  imagem `frrouting/frr:latest`).

## Scripts disponíveis

### `inject_loss.sh`

```
./scripts/inject_loss.sh <roteador|local> <iface|auto> <perda_%> [<delay_ms>] [<jitter_ms>]
```

Exemplo com perda, atraso e jitter:

```
./scripts/inject_loss.sh r1 eth0 10 50 10
```

Exemplo apenas com perda detectando automaticamente a interface principal
(dentro do container `r2`):

```
./scripts/inject_loss.sh r2 auto 5
```

- Valida parâmetros numéricos e o estado da interface.
- Detecta automaticamente a interface quando `auto` é informado.
- Atualiza uma `qdisc netem` existente utilizando `tc qdisc change`, mantendo o
  comportamento idempotente.
- Exibe mensagens claras quando `docker` ou `tc` não estão disponíveis.

### `recover_link.sh`

```
./scripts/recover_link.sh <roteador|local> <iface|auto>
```

Remove a `qdisc` aplicada previamente. Caso não exista uma `qdisc`, o script
informa que não há nada para remover e retorna sucesso.

Exemplo:

```
./scripts/recover_link.sh r1 eth0
```

### `inject_flap.sh` (opcional)

```
./scripts/inject_flap.sh <roteador|local> <iface|auto> <tempo_down_s> <tempo_up_s> [<ciclos>]
```

Simula um flap na interface alternando os estados *down* e *up*.

Exemplo executando dois ciclos de 3 segundos down / 5 segundos up:

```
./scripts/inject_flap.sh r3 eth1 3 5 2
```

### `test_injection.sh`

Script auxiliar que sobe o ambiente com Docker Compose, aplica uma perda de
pacotes em `r1:eth0`, executa `ping` entre `h1` e `h2`, recupera o link e roda um
novo `ping`. Os resultados são salvos em `results/`.

```
./scripts/test_injection.sh
```

> **Importante:** o script assume que já existe um `docker-compose.yml` gerado e
> que os containers `h1` e `h2` estão definidos nele.

## Dicas de validação manual

1. Gere o arquivo `docker-compose.yml`:

   ```bash
   python3 generate_compose.py
   ```

2. Suba os containers:

   ```bash
   docker-compose up -d
   ```

3. Aplique uma perda de 20% na interface `eth0` do roteador `r1`:

   ```bash
   ./scripts/inject_loss.sh r1 eth0 20
   ```

4. Verifique a configuração aplicada:

   ```bash
   docker exec r1 tc qdisc show dev eth0
   ```

5. Recupere o link:

   ```bash
   ./scripts/recover_link.sh r1 eth0
   ```

6. Confirme que a `qdisc` foi removida:

   ```bash
   docker exec r1 tc qdisc show dev eth0
   ```

Esses passos garantem que os scripts funcionam corretamente e podem ser
reaplicados quantas vezes forem necessários.
