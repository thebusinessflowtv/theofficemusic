# The Office Music — AI Music Pipeline

Pipeline para gerar faixas instrumentais originais do canal **The Office Music** usando **Stable Audio 3.0 Medium self-hosted**.

## Objetivo

- Gerar músicas 100% por texto, sem usar músicas comerciais ou arquivos de áudio como fonte.
- Manter um DNA musical consistente para office, home office, retail/lounge e ambientes de trabalho.
- Gerar 4–5 faixas por lote.
- Executar a geração em GPU no Kaggle.
- Entregar WAVs individuais e um manifesto com prompts, seeds e metadados.
- Em uma etapa posterior, montar os WAVs em uma sessão de 1 hora e combinar com um único vídeo visual em loop.

## Arquitetura

1. `config/channel_profile.yaml` — DNA musical do canal.
2. `src/prompt_engine.py` — transforma o DNA musical em prompts variados, mas coerentes.
3. `src/generate_tracks.py` — geração text-to-audio pura com Stable Audio 3 Medium.
4. `kaggle/runner.py` — runner para GPU Kaggle.
5. `.github/workflows/generate-music.yml` — dispara o job Kaggle manualmente pelo GitHub Actions.

## Princípio de originalidade

O pipeline padrão usa apenas `model.generate(prompt=...)`. Não usa `init_audio`, áudio comercial, samples externos, stems de terceiros ou audio-to-audio. Cada faixa parte de ruído aleatório e de um prompt textual.

## Modelo

- Modelo: `stabilityai/stable-audio-3-medium`
- Saída: estéreo 44.1 kHz
- Duração máxima por geração: 380 s
- Hardware: CUDA + Flash Attention 2

## Segredos necessários

### GitHub Actions

- `KAGGLE_API_TOKEN`
- `KAGGLE_USERNAME`

### Kaggle Secrets

- `HF_TOKEN`

O `HF_TOKEN` precisa pertencer a uma conta Hugging Face que tenha aceitado os termos do modelo `stabilityai/stable-audio-3-medium`.

## Status

Estrutura inicial criada. O arquivo de perfil musical será calibrado com as respostas do proprietário do canal antes da primeira geração oficial.
