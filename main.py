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


def obter_urls_com_playwright():
    """Abre um navegador headless, percorre todas as páginas de paginação do catálogo
    e captura os links de produto de cada uma, parando quando uma página não trouxer
    nenhum produto novo (fim da paginação ou página inexistente)."""
    print("🚀 Abrindo o navegador invisível para renderizar o catálogo...")
    urls_produtos = set()
    base_netloc = urlparse(BASE_URL).netloc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

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

        browser.close()

    lista_urls = sorted(list(urls_produtos))
    print(f"✅ Encontrados {len(lista_urls)} produtos após renderização do JavaScript!\n")
    return lista_urls


def normalizar_maiusculas(texto):
    """Corrige a recomendação 'Content is in uppercase' da Meta: qualquer
    palavra inteiramente em maiúsculas (2+ letras) vira Capitalizada
    (primeira letra maiúscula, resto minúsculo). Números, %, siglas de uma
    letra e texto que já não está em caixa alta ficam intocados."""
    if not texto:
        return texto

    def cap_palavra(m):
        palavra = m.group(0)
        if len(palavra) > 1 and palavra.isupper():
            return palavra[0] + palavra[1:].lower()
        return palavra

    return re.sub(r"\w+", cap_palavra, texto, flags=re.UNICODE)


def limpar_descricao(raw):
    """Limpa a descrição crua vinda do JSON-LD desse site, que costuma vir sem
    formatação real: rótulos 'Sobre essa peça:'/'Composição:'/'Descrição:' colados
    no texto ao redor, espaços especiais (nbsp) e, às vezes, o rótulo 'Descrição:'
    penduradinho no final sem nenhum conteúdo depois."""
    if not raw or not raw.strip():
        return "Marias & Tal"

    texto = raw.replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"(?i)^sobre essa peça:\s*", "", texto).strip()

    m_comp = re.search(r"(?i)composição:\s*(.*?)(?=descrição:?|$)", texto)
    m_desc = re.search(r"(?i)descrição:?\s*(.*)$", texto)

    composicao = m_comp.group(1).strip(" .,;") if m_comp else ""
    descricao = m_desc.group(1).strip(" .,;") if m_desc else ""

    partes = []
    if composicao:
        partes.append(f"Composição: {composicao}.")
    if descricao:
        partes.append(descricao if descricao.endswith((".", "!", "?")) else f"{descricao}.")

    if not partes:
        # Texto simples, sem os rótulos "Composição"/"Descrição" (ex.: "Bermuda com bordado na lateral")
        texto = texto.strip(" .,;")
        partes.append(f"{texto}.")

    resultado = re.sub(r"\s+", " ", " ".join(partes)).strip()
    resultado = normalizar_maiusculas(resultado)
    return resultado or "Marias & Tal"


def determinar_disponibilidade(soup):
    """Decide o status real de estoque a partir do DOM já renderizado (pós-JS).
    O JSON-LD desse site frequentemente NÃO traz o campo 'availability', então
    não dá pra confiar nele — o sinal confiável é o estado do botão de compra
    (fica com o atributo 'disabled' quando esgotado) e/ou o texto de aviso
    '.block-product__stock-text' (existe no DOM mesmo quando escondido por CSS)."""
    btn = soup.find("button", attrs={"data-qa": "productsection-btn-addtobag"})
    if btn and btn.has_attr("disabled"):
        return "out of stock"

    stock_text_el = soup.find(class_="block-product__stock-text")
    if stock_text_el:
        texto = stock_text_el.get_text(strip=True).lower()
        if "indispon" in texto or "esgotado" in texto:
            return "out of stock"

    return "in stock"


def verificar_produto(page, url):
    """Abre a página do produto no navegador (JS executado), espera a checagem
    de estoque assíncrona terminar, e extrai os dados combinando JSON-LD
    (título, descrição, preço, imagem) com o DOM renderizado (disponibilidade real)."""
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)  # dá tempo pro estado de estoque assíncrono terminar de carregar

    soup = BeautifulSoup(page.content(), "html.parser")
    script_tag = soup.find("script", type="application/ld+json")

    if not (script_tag and script_tag.string):
        return None

    data = json.loads(script_tag.string)
    offers = data.get("offers", {})
    availability = determinar_disponibilidade(soup)

    return {
        "id": url.split("/")[-1],
        "title": normalizar_maiusculas(data.get("name")),
        "description": limpar_descricao(data.get("description", "")),
        "availability": availability,
        "condition": "new",
        "price": f"{offers.get('price', '')} BRL" if offers.get("price") else "",
        "link": offers.get("url", url),
        "image_link": data.get("image"),
        "brand": "Marias & Tal",
        "inventory": 1 if availability == "in stock" else 0,  # exigido pela Meta mesmo sem controle numérico real
    }


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
    urls_atuais = obter_urls_com_playwright()
    produtos_conhecidos = carregar_estado_anterior()

    # Precisamos checar tanto os produtos visíveis agora quanto os que já
    # conhecíamos de execuções anteriores (para saber se ainda existem ou não).
    urls_conhecidas = {f"{BASE_URL}/{pid}" for pid in produtos_conhecidos.keys()}
    todas_urls = sorted(set(urls_atuais) | urls_conhecidas)

    if not todas_urls:
        print("❌ Nenhum produto foi capturado (nem novo, nem no histórico). Verifique os seletores.")
        return

    produtos_dict = {}
    novos_no_historico = len(urls_conhecidas - set(urls_atuais))
    print(f"📦 Verificando {len(todas_urls)} produtos "
          f"({len(urls_atuais)} visíveis agora + {novos_no_historico} só no histórico)...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        for idx, url in enumerate(todas_urls, 1):
            product_id = url.split("/")[-1]
            item_obtido = None

            try:
                item_obtido = verificar_produto(page, url)
            except Exception as e:
                print(f"[{idx}/{len(todas_urls)}] ✖ Erro ao acessar {url}: {e}")

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
                item_anterior["inventory"] = 0
                produtos_dict[product_id] = item_anterior
                print(f"[{idx}/{len(todas_urls)}] ⚪ {item_anterior.get('title', product_id)} -> "
                      f"não encontrado no site agora, mantido como out of stock")

            else:
                # Nunca vimos esse produto antes e agora ele também não respondeu:
                # não há dados pra montar uma linha, então ignoramos.
                print(f"[{idx}/{len(todas_urls)}] ⚠ {url} inacessível e sem histórico — ignorado")

        browser.close()

    if produtos_dict:
        fieldnames = ["id", "title", "description", "availability", "inventory", "condition", "price", "link", "image_link", "brand"]
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