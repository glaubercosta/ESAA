# ESAA Semantic Memory Read-Model Specification

## Objetivo
Definir a projeção assíncrona do Event Store para um Vector DB, permitindo que os agentes acessem experiências passadas de forma semântica (RAG-native).

## Escopo
- **Gatilho de Projeção**: Eventos do tipo `task.complete` e `review.approve`.
- **Estratégia de Embedding**: Fragmentação de notas e file_updates em pedaços (chunks) indexáveis.
- **Armazenamento**: Vetores com metadados (task_id, actor, ts, outcome).

## Requisitos
1. **Determinismo**: A projeção deve ser reconstituível a partir do `activity.jsonl` a qualquer momento.
2. **Camadas de Contexto**:
   - `Short-term`: Roadmap atual e contexto da tarefa.
   - `Long-term`: Memória semântica recuperada via similaridade de cossenos.
3. **Privacidade**: Empregar hashes para anonimizar informações sensíveis no Vector DB, se necessário.

## Critérios de Aceitação
- [ ] Protótipo do projetor que lê o `activity.jsonl` e gera um dump de vetores.
- [ ] Interface de busca (Search Tool) que recebe uma query e retorna o top-K contexto relevante.
- [ ] Auditoria: Cada entrada no Vector DB deve referenciar o `event_id` de origem.

