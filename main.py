import html
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_BASE_URL = "https://pncp.gov.br/api/consulta"
ENDPOINT = f"{API_BASE_URL}/v1/contratacoes/proposta"
DB_NAME = os.getenv("DB_NAME", "licitacoes_geociencias.db")
BR_TZ = ZoneInfo("America/Sao_Paulo")
PAGE_SIZE = 50
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))
DIAS_A_FRENTE = int(os.getenv("DIAS_A_FRENTE", "30"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MODALIDADES = {
    1: "Leilão",
    2: "Concorrência",
    3: "Concurso",
    4: "Pregão",
    5: "Diálogo Competitivo",
    6: "Dispensa de Licitação",
    7: "Inexigibilidade",
}

# A normalização remove diferenças entre caracteres acentuados e não acentuados.
TERMOS = (
    "geologia", "geologo", "geologica", "geologicas", "geologico", "geologicos",
    "geotecnia", "geotecnico", "geotecnica", "hidrogeologia", "hidrogeologico",
    "geofisica", "geofisico", "poco artesiano", "pocos artesianos",
    "perfuracao de poco", "poco de monitoramento", "sondagem a trado",
    "sondagem rotativa", "sondagem spt", "sondagem mista", "sondagem percussao",
    "estabilidade de talude", "contencao de encosta", "mapeamento geologico",
    "ensaio de solo", "mecanica dos solos", "outorga de agua",
    "rebaixamento de lencol", "topografia", "topografico",
    "georreferenciamento", "georreferenciado", "aerofotogrametria",
    "meio ambiente", "ambiental", "licenciamento ambiental",
    "estudo de impacto", "eia rima", "recuperacao de area degradada",
    "prad", "barragem", "barragens", "recursos hidricos",
    "aguas subterraneas", "teste de bombeamento",
)
TERMOS_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in TERMOS) + r")\b",
    re.IGNORECASE,
)


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


def objeto_relevante(objeto):
    return bool(TERMOS_REGEX.search(normalizar(objeto)))


def inicializar_banco():
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licitacoes (
                id_pncp TEXT PRIMARY KEY,
                orgao TEXT NOT NULL,
                objeto TEXT NOT NULL,
                valor REAL,
                link TEXT,
                data_descoberta TEXT NOT NULL
            )
        """)
        conn.commit()


def licitacao_ja_existe(id_pncp):
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        return conn.execute(
            "SELECT 1 FROM licitacoes WHERE id_pncp = ?", (id_pncp,)
        ).fetchone() is not None


def salvar_licitacao(id_pncp, orgao, objeto, valor, link):
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO licitacoes
               (id_pncp, orgao, objeto, valor, link, data_descoberta)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (id_pncp, orgao, objeto, valor, link,
             datetime.now(BR_TZ).isoformat()),
        )
        conn.commit()


def obter_id(item):
    # numeroControlePNCP é o identificador mais apropriado quando fornecido.
    numero = item.get("numeroControlePNCP")
    if numero:
        return str(numero)

    cnpj = item.get("cnpjOrgao") or item.get("orgaoEntidade", {}).get("cnpj")
    ano = item.get("anoCompra")
    sequencial = item.get("sequencialCompra")
    if cnpj and ano is not None and sequencial is not None:
        return f"{cnpj}-{ano}-{sequencial}"

    raise ValueError("Registro PNCP sem identificador suficiente para deduplicação")


def montar_link(item):
    link = item.get("linkSistemaOrigem") or item.get("urlItemControle")
    if link and str(link).startswith(("https://", "http://")):
        return str(link)

    numero = item.get("numeroControlePNCP")
    if numero:
        return f"https://pncp.gov.br/app/editais/{numero}"

    cnpj = item.get("cnpjOrgao") or item.get("orgaoEntidade", {}).get("cnpj")
    ano = item.get("anoCompra")
    sequencial = item.get("sequencialCompra")
    if cnpj and ano is not None and sequencial is not None:
        return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}"
    return "https://pncp.gov.br/"


def enviar_alerta(mensagem, session=requests):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID não configurados. "
            "Cadastre ambos como variáveis de ambiente/Secrets."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = session.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensagem,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram recusou a mensagem: {payload}")
    return payload


def formatar_mensagem(item):
    orgao_obj = item.get("orgaoEntidade") or {}
    orgao = orgao_obj.get("razaoSocial") or item.get("nomeOrgao") or "Órgão não identificado"
    objeto = item.get("objeto") or item.get("objetoCompra") or "Objeto não informado"
    valor = item.get("valorTotalEstimado")
    try:
        valor_txt = f"R$ {float(valor or 0):,.2f}"
    except (TypeError, ValueError):
        valor_txt = "Não informado"
    modalidade_codigo = item.get("codigoModalidadeContratacao")
    modalidade = MODALIDADES.get(modalidade_codigo, f"Código {modalidade_codigo or 'não informado'}")
    link = montar_link(item)

    # Escapar conteúdo dinâmico para não quebrar o HTML do Telegram.
    return (
        "🚨 <b>OPORTUNIDADE GEOCIÊNCIAS</b>\n\n"
        f"🏛 <b>Órgão:</b> {html.escape(str(orgao))}\n"
        f"🛠 <b>Modalidade:</b> {html.escape(modalidade)}\n"
        f"💰 <b>Valor estimado:</b> {html.escape(valor_txt)}\n"
        f"📝 <b>Objeto:</b>\n{html.escape(str(objeto))[:2500]}\n"
        f'🔗 <a href="{html.escape(link, quote=True)}">Acesse a contratação no PNCP</a>'
    )


def consultar_pagina(session, data_final, pagina):
    params = {
        "dataFinal": data_final,
        "pagina": pagina,
        "tamanhoPagina": PAGE_SIZE,
    }
    response = session.get(
        ENDPOINT,
        params=params,
        headers={"Accept": "application/json", "User-Agent": "GeoIntelBrasil-Monitor/1.0"},
        timeout=(10, 45),
    )
    if response.status_code == 204:
        return {"data": [], "totalPaginas": pagina}
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Resposta inesperada da API PNCP: JSON não é objeto")
    if "data" not in payload or not isinstance(payload["data"], list):
        raise ValueError(f"Resposta PNCP sem lista 'data': {payload}")
    return payload


def processar_oportunidades(session=requests):
    inicializar_banco()
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Configure TELEGRAM_TOKEN e TELEGRAM_CHAT_ID antes de executar.")

    hoje = datetime.now(BR_TZ).date()
    data_final = (hoje + timedelta(days=DIAS_A_FRENTE)).strftime("%Y%m%d")
    logging.info("Consultando propostas abertas no PNCP até %s", data_final)

    total_lidos = 0
    total_relevantes = 0
    total_enviados = 0
    pagina = 1

    while pagina <= MAX_PAGES:
        payload = None
        for tentativa in range(1, 4):
            try:
                payload = consultar_pagina(session, data_final, pagina)
                break
            except (requests.RequestException, ValueError) as exc:
                logging.warning("Falha PNCP página %s, tentativa %s/3: %s",
                                pagina, tentativa, exc)
                if tentativa == 3:
                    raise
                time.sleep(2 ** tentativa)

        itens = payload["data"]
        if not itens:
            break

        total_lidos += len(itens)
        for item in itens:
            objeto = item.get("objeto") or item.get("objetoCompra") or ""
            if not objeto_relevante(objeto):
                continue

            total_relevantes += 1
            id_pncp = obter_id(item)
            if licitacao_ja_existe(id_pncp):
                continue

            mensagem = formatar_mensagem(item)
            # Só registra no banco depois da confirmação positiva do Telegram.
            enviar_alerta(mensagem, session=session)
            orgao = (item.get("orgaoEntidade") or {}).get("razaoSocial", "Órgão não identificado")
            try:
                valor = float(item.get("valorTotalEstimado") or 0)
            except (TypeError, ValueError):
                valor = None
            salvar_licitacao(id_pncp, orgao, objeto, valor, montar_link(item))
            total_enviados += 1
            logging.info("Alerta enviado: %s | %s", orgao, id_pncp)

        total_paginas = payload.get("totalPaginas")
        if total_paginas is not None and pagina >= int(total_paginas):
            break
        if len(itens) < PAGE_SIZE:
            break
        pagina += 1

    logging.info(
        "Concluído: %s registros lidos, %s relevantes, %s alertas enviados.",
        total_lidos, total_relevantes, total_enviados,
    )
    return {
        "lidos": total_lidos,
        "relevantes": total_relevantes,
        "enviados": total_enviados,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    processar_oportunidades()
