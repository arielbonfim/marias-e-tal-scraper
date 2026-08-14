import csv
import json
import os
import re
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://mariasetal.com"
CATALOG_URL = f"{BASE_URL}/produtos"
STATE_FILE = "produtos_conhecidos.json"  # histórico entre execuções: precisa ser commitado no repo

EXCLUDED_SLUGS = {
    "", "produtos", "novidades", "vestidos", "bazar", "home",
    "liqui", "colecao", "trocas-e-cuidados", "sobre", "termos-e-condicoes"
}

PAGINATION_PARAM = "store-page-ai-YU6n3j"  # parâmetro de paginação usado pelo builder da Hostinger
MAX_PAGINAS = 50  # trava de segurança para nunca entrar num loop infinito

# Seletores descobertos no HTML renderizado do site (ver análise do 317068-vestido-lais):
# o JSON-LD frequentemente OMITE o campo "offers.availability" quando o produto está
# esgotado, e o script antigo tratava omissão como "in stock" por padrão (bug).
# O estado real só aparece no DOM depois que o JS da página roda: o botão de compra
# fica com o atributo "disabled" e existe um <p> com o texto "Indisponível" (que fica
# visualmente escondido via CSS, mas continua presente no DOM/texto).
SELETOR_BOTAO_COMPRAR = '[data-qa="productsection-btn-addtobag"]'
SELETOR_TEXTO_ESTOQUE = '.block-product__stock-text'
TEXTO_INDISPONIVEL = "indispon"  # cobre "Indisponível" ignorando maiúsculas/acentos parciais


def _extrair_urls_produto(html, base_netloc):
    """Recebe o HTML já renderizado de uma página e devolve o set de URLs de produto encontradas nela."""
    soup = BeautifulSoup(html, "html.parser")
    encontrados = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        # Resolve o link (absoluto ou relativo) para uma URL absoluta e depois
        # extrai só o path — resolve o bug de hrefs absolutos tipo
        # "https://mariasetal.com/317033-vestido-maria-zoe"
        full_url = urljoin(BASE_URL, href)
        parsed = urlparse(full_url)

        # Ignora links externos (redes sociais, whatsapp, etc.)
        if parsed.netloc != base_netloc:
            continue

        slug = parsed.path.split("?")[0].strip("/")

        # Captura links no padrão de produtos (ex: 317033-vestido-maria-zoe)
        if slug and slug not in EXCLUDED_SLUGS and "/" not in slug:
            if re.search(r"^[0-9]{5,6}-", slug):
                encontrados.add(f"{BASE_URL}/{slug}")

    return encontrados


def obter_urls_com_playwright(page):
    """Percorre todas as páginas de paginação do catálogo e captura os links de produto
    de cada uma, parando quando uma página não trouxer nenhum produto novo (fim da
    paginação ou página inexistente). Recebe uma `page` já aberta para reaproveitar
    a mesma sessão de navegador usada depois para checar cada produto."""
    print("🚀 Percorrendo o catálogo para descobrir os produtos...")
    urls_produtos = set()
    base_netloc = urlparse(BASE_URL).netloc

    for pagina in range(1, MAX_PAGINAS + 1):
        url_pagina = CATALOG_URL if pagina == 1 else f"{CATALOG_URL}?{PAGINATION_PARAM}={pagina}"

        page.goto(url_pagina, wait_until="networkidle", timeout=60000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        html_renderizado = page.content()

        encontrados = _extrair_urls_produto(html_renderizado, base_netloc)
        antes = len(urls_produtos)
        urls_produtos.update(encontrados)
        novos = len(urls_produtos) - antes

        print(f"   📄 Página {pagina}: {len(encontrados)} produtos na página, {novos} novos no total")

        # Se a página não trouxe nenhum produto que já não estivesse na lista,
        # assumimos que a paginação acabou (ou o site redirecionou pra página 1).
        if novos == 0:
            break

    lista_urls = sorted(list(urls_produtos))
    print(f"✅ Encontrados {len(lista_urls)} produtos após renderização do JavaScript!\n")
    return lista_urls


def _limpar_descricao(texto):
    """Corrige a formatação da descrição vinda do JSON-LD do site.

    O builder da Hostinger frequentemente junta blocos de texto (ex: "Sobre essa
    peça:", "Composição:", "Descrição:") sem espaço entre eles, e usa '\\xa0'
    (non-breaking space) em vez de espaço comum — o que resulta em textos como
    "Composição:\\xa0100% VISCOSEDescrição:Vestido..." quando lido em Python.
    Isso vai parar sem tratamento no feed do Instagram/Facebook, então precisa
    ser corrigido antes de gerar o CSV.
    """
    if not texto:
        return texto

    t = texto.replace("\xa0", " ")

    # Insere espaço depois de ':' ou '.' quando está colado na palavra seguinte
    # (ex: "peça:Composição" -> "peça: Composição")
    t = re.sub(r"([:.])(?=[^\s])", r"\1 ", t)

    # Insere espaço quando uma palavra (minúscula ou sigla em CAIXA-ALTA) está
    # colada direto numa palavra capitalizada seguinte, sem nenhuma pontuação
    # entre elas (ex: "ALGODÃODescrição" -> "ALGODÃO Descrição")
    t = re.sub(r"(?<=[a-zà-úA-ZÀ-Ú0-9%])(?=[A-ZÀ-Ú][a-zà-ú])", " ", t)

    # Colapsa espaços múltiplos resultantes e limpa as pontas
    t = re.sub(r"\s+", " ", t).strip()

    # Remove "Descrição:" (ou variações de maiúsc/minúsc e espaço antes dos dois
    # pontos) quando ela sobra no final do texto sem nenhum conteúdo depois —
    # nesses casos o campo não agrega nada e só polui a descrição.
    t = re.sub(r"\s*Descrição\s*:\s*$", "", t, flags=re.IGNORECASE).strip()

    return t


def _checar_estoque_no_dom(page):
    """Lê o estado REAL de estoque a partir do DOM já renderizado (pós-JS), em vez do
    JSON-LD. Retorna "in stock" ou "out of stock".

    Regra: se o botão de comprar estiver desabilitado OU existir o texto "Indisponível"
    (mesmo que escondido via CSS) associado ao produto, consideramos esgotado.
    Qualquer um dos dois sinais basta — não exigimos os dois ao mesmo tempo, porque
    não temos garantia de que o site sempre renderiza ambos de forma consistente.
    """
    esgotado = False

    try:
        botao = page.query_selector(SELETOR_BOTAO_COMPRAR)
        if botao is not None:
            # is_disabled() considera tanto o atributo "disabled" quanto aria-disabled
            if botao.is_disabled():
                esgotado = True
    except Exception:
        pass

    if not esgotado:
        try:
            texto_estoque = page.query_selector(SELETOR_TEXTO_ESTOQUE)
            if texto_estoque is not None:
                conteudo = (texto_estoque.text_content() or "").strip().lower()
                if TEXTO_INDISPONIVEL in conteudo:
                    esgotado = True
        except Exception:
            pass

    return "out of stock" if esgotado else "in stock"


def _verificar_produto_playwright(page, url):
    """Visita a página de um produto com o navegador (JS habilitado) e monta o item
    do catálogo. Usa o JSON-LD para os campos descritivos (título, preço, imagem etc,
    que não têm o problema de disponibilidade) e o DOM renderizado para o campo
    `availability`, que é o único que o JSON-LD não reporta de forma confiável."""
    try:
        response = page.goto(url, wait_until="networkidle", timeout=45000)
        if response is None or response.status >= 400:
            return None

        # Dá um tempo extra para a chamada assíncrona que atualiza o estado de
        # disponibilidade dos botões terminar de rodar (ela acontece depois do
        # "networkidle" inicial, conforme identificado na análise do HTML).
        page.wait_for_timeout(1500)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        script_tag = soup.find("script", type="application/ld+json")

        if not script_tag or not script_tag.string:
            return None

        data = json.loads(script_tag.string)
        offers = data.get("offers", {})

        availability = _checar_estoque_no_dom(page)

        product_id = url.split("/")[-1]
        return {
            "id": product_id,
            "title": data.get("name"),
            "description": _limpar_descricao(data.get("description", "Marias & Tal")),
            "availability": availability,
            "condition": "new",
            "price": f"{offers.get('price', '')} BRL" if offers.get("price") else "",
            "link": offers.get("url", url),
            "image_link": data.get("image"),
            "brand": "Marias & Tal"
        }
    except Exception as e:
        print(f"   ✖ Erro ao acessar {url}: {e}")
        return None


def carregar_estado_anterior():
    """Carrega o catálogo salvo na execução anterior (id -> item), se existir.
    É esse histórico que permite detectar produtos que saíram do site."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠ Não foi possível ler {STATE_FILE} ({e}), começando do zero.")
    return {}


def salvar_estado(produtos_dict):
    """Salva o catálogo atual (id -> item) para a próxima execução conseguir comparar."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(produtos_dict, f, ensure_ascii=False, indent=2)


def gerar_catalogo():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        urls_atuais = obter_urls_com_playwright(page)
        produtos_conhecidos = carregar_estado_anterior()

        # Precisamos checar tanto os produtos visíveis agora quanto os que já
        # conhecíamos de execuções anteriores (para saber se ainda existem ou não).
        urls_conhecidas = {f"{BASE_URL}/{pid}" for pid in produtos_conhecidos.keys()}
        todas_urls = sorted(set(urls_atuais) | urls_conhecidas)

        if not todas_urls:
            print("❌ Nenhum produto foi capturado (nem novo, nem no histórico). Verifique os seletores.")
            browser.close()
            return

        produtos_dict = {}
        novos_no_historico = len(urls_conhecidas - set(urls_atuais))
        print(f"📦 Verificando {len(todas_urls)} produtos "
              f"({len(urls_atuais)} visíveis agora + {novos_no_historico} só no histórico)...\n")

        for idx, url in enumerate(todas_urls, 1):
            product_id = url.split("/")[-1]
            item_obtido = _verificar_produto_playwright(page, url)

            if item_obtido:
                # Página respondeu normalmente: usa os dados frescos (inclui o
                # caso de já estar "out of stock" mas ainda publicada no site).
                status_icon = "🟢" if item_obtido["availability"] == "in stock" else "🔴"
                produtos_dict[product_id] = item_obtido
                print(f"[{idx}/{len(todas_urls)}] {status_icon} {item_obtido['title']} -> {item_obtido['availability']}")

            elif product_id in produtos_conhecidos:
                # Produto sumiu do site (removido/despublicado) mas já existia
                # antes: mantém a linha com os últimos dados conhecidos, marcada
                # como out of stock em vez de simplesmente sumir do CSV.
                item_anterior = dict(produtos_conhecidos[product_id])
                item_anterior["availability"] = "out of stock"
                produtos_dict[product_id] = item_anterior
                print(f"[{idx}/{len(todas_urls)}] ⚪ {item_anterior.get('title', product_id)} -> "
                      f"não encontrado no site agora, mantido como out of stock")

            else:
                # Nunca vimos esse produto antes e agora ele também não respondeu:
                # não há dados pra montar uma linha, então ignoramos.
                print(f"[{idx}/{len(todas_urls)}] ⚠ {url} inacessível e sem histórico — ignorado")

        browser.close()

    if produtos_dict:
        fieldnames = ["id", "title", "description", "availability", "condition", "price", "link", "image_link", "brand"]
        with open("catalog.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted(produtos_dict.values(), key=lambda p: p["id"]))

        salvar_estado(produtos_dict)

        em_estoque = sum(1 for p in produtos_dict.values() if p["availability"] == "in stock")
        esgotados = len(produtos_dict) - em_estoque
        print(f"\n🎉 Sucesso! 'catalog.csv' gerado com {len(produtos_dict)} produtos "
              f"({em_estoque} em estoque, {esgotados} esgotados/removidos).")
    else:
        print("\n❌ Nítida falha ao estruturar os dados do catálogo.")


if __name__ == "__main__":
    gerar_catalogo()