# PDF Translator

Aplicacao web gratuita para traduzir documentos PDF preservando a formatacao original, incluindo imagens, logos, tabelas e layout.

Utiliza Google Translate (sem necessidade de API key ou pagamento) e PyMuPDF para manipulacao de PDFs.

## Funcionalidades

- Upload de PDF via drag-and-drop ou selecao de arquivo
- Deteccao automatica do idioma de origem
- Suporte a mais de 100 idiomas de destino
- Preserva formatacao, fontes, cores, imagens, logos, tabelas e layout do documento original
- Ajuste automatico do tamanho do texto traduzido para caber no espaco original
- Interface web moderna e responsiva
- 100% gratuito, sem APIs pagas

## Pre-requisitos

- Python 3.9 ou superior
- pip (gerenciador de pacotes do Python)
- Git

## Instalacao passo a passo

### 1. Clonar o repositorio

```bash
git clone https://github.com/luizbastos08/pdf-translator.git
cd pdf-translator
```

### 2. Criar um ambiente virtual (recomendado)

**Linux / macOS:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**

```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Instalar as dependencias

```bash
pip install -r requirements.txt
```

### 4. Iniciar a aplicacao

```bash
python app.py
```

A aplicacao estara disponivel em: **http://localhost:5000**

## Como usar

1. Abra o navegador e acesse `http://localhost:5000`
2. Arraste um arquivo PDF para a area de upload ou clique para selecionar
3. Selecione o idioma de origem (opcional — a deteccao automatica funciona na maioria dos casos)
4. Selecione o idioma de destino
5. Clique em **Traduzir PDF**
6. Aguarde o processamento — o download do PDF traduzido sera iniciado automaticamente

## Estrutura do projeto

```
pdf-translator/
├── app.py                 # Backend Flask (servidor e logica de traducao)
├── requirements.txt       # Dependencias Python
├── templates/
│   └── index.html         # Frontend (interface web)
└── README.md
```

## Tecnologias utilizadas

| Tecnologia | Finalidade |
|---|---|
| Flask | Servidor web |
| PyMuPDF (fitz) | Leitura, manipulacao e escrita de PDFs |
| deep-translator | Traducao gratuita via Google Translate |

## Limitacoes

- PDFs com texto rasterizado (imagens de texto) nao serao traduzidos — apenas texto selecionavel e processado
- Documentos muito grandes podem levar alguns minutos para serem traduzidos devido aos limites de requisicoes do Google Translate
- Fontes especiais do documento original sao substituidas por Helvetica (normal, bold, italic) no PDF traduzido
- O tamanho maximo de upload e 50 MB
