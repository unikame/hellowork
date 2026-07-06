import streamlit as st
import time
import re
import os
import shutil
import base64 as _b64
import urllib.parse
import gspread
from gspread_formatting import get_effective_format
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from bs4 import BeautifulSoup

# ＝＝＝ 検索条件スプレッドシート ＝＝＝
MAIN_SHEET_KEY = "1vhIfDZ1_GjLGspclN6ZvutdbWitdMO9qx6Aqhh9B4cQ"
MAIN_SHEET_GID = 1003420488  # 「求人依頼シート」

HW_SEARCH_URL = "https://www.hellowork.mhlw.go.jp/kensaku/GECA110010.do?action=initDisp&screenId=GECA110010"

st.set_page_config(page_title="ASUMO ハローワーク求人取得", page_icon="🪁",
                   layout="wide", initial_sidebar_state="expanded")


def setup_browser():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_path = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    driver_path = shutil.which("chromedriver") or shutil.which("chromium-driver")
    if chrome_path:
        options.binary_location = chrome_path
    if driver_path:
        driver = webdriver.Chrome(service=Service(driver_path), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    return driver


# ＝＝＝ 職種 大項目名 → モーダルsuffix ＝＝＝
HW_SHOKUSYU_DAI = [
    (["営業"], "01"),
    (["販売"], "02"),
    (["飲食", "フード"], "03"),
    (["事務", "オフィス"], "04"),
    (["警備", "ビル"], "05"),
    (["教育", "保育"], "06"),
    (["管理職", "経営", "金融", "保険"], "07"),
    (["医療", "保健"], "08"),
    (["介護", "福祉"], "09"),
    (["ドライバー", "配達", "運転"], "10"),
    (["IT", "Web", "エンジニア"], "11"),
    (["製造", "工場"], "12"),
    (["清掃", "軽作業"], "13"),
    (["建設", "土木"], "14"),
    (["農林", "水産", "農業"], "15"),
    (["理容", "美容"], "16"),
]


def _norm(s):
    """全角記号・空白を除いた正規化（テキスト照合用）"""
    if not s:
        return ""
    return re.sub(r'[\s　・/／（）\(\)]', '', s)


def hw_click(driver, element):
    """要素を確実にクリック（JSクリックでオーバーレイ回避）"""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    time.sleep(0.2)
    driver.execute_script("arguments[0].click();", element)


def _jstext(driver, el):
    """非表示要素でも取得できる textContent をJS経由で取得"""
    try:
        return driver.execute_script("return arguments[0].textContent || '';", el)
    except Exception:
        return ""


def hw_select_kubun(driver, kubun, kinmu, log_area):
    """
    求人区分（C列＝一般求人 等）と勤務時間（D列＝パート/フルタイム）を設定。
    C列が空欄の行はそもそも呼ばれない（呼び出し側でスキップ）。
    """
    # 一般求人ラジオ（既定でchecked）
    try:
        radio = driver.find_element(By.ID, "ID_kjKbnRadioBtn1")
        if not radio.is_selected():
            hw_click(driver, radio)
            time.sleep(0.3)
    except Exception:
        pass
    # 勤務時間（D列）：パート / フルタイム
    if kinmu and "パート" in kinmu:
        try:
            part = driver.find_element(By.ID, "ID_ippanCKBox2")
            if not part.is_selected():
                hw_click(driver, part)
            log_area.text("   求人区分：一般求人＋パート を選択しました。")
        except Exception as e:
            log_area.warning(f"   パートのチェックに失敗: {e}")
    elif kinmu and "フルタイム" in kinmu:
        try:
            full = driver.find_element(By.ID, "ID_ippanCKBox1")
            if not full.is_selected():
                hw_click(driver, full)
            log_area.text("   求人区分：一般求人＋フルタイム を選択しました。")
        except Exception as e:
            log_area.warning(f"   フルタイムのチェックに失敗: {e}")


def hw_select_area(driver, pref, mid_cat, cities, log_area):
    """
    就業場所モーダルで 都道府県→中分類→市区町村（最大5つ）を選択して「決定」。
    pref(F列)=埼玉県, mid_cat(G列)=埼玉県市部, cities(H〜L列)=[草加市, 越谷市, 八潮市, 三郷市, ...]
    すべて表示テキストで照合（IDは不安定なため）。
    """
    try:
        btn = driver.find_element(By.ID, "ID_todohukenHiddenAccoBtn")
        hw_click(driver, btn)
    except Exception as e:
        log_area.warning(f"   都道府県モーダルを開けませんでした: {e}")
        return False

    # モーダルの中身がJS生成されるまで待つ（最大10秒）
    appeared = False
    for _ in range(20):
        time.sleep(0.5)
        cnt = driver.execute_script("return document.querySelectorAll('button.ac_headerTwo').length;")
        if cnt and cnt > 0:
            appeared = True
            break
    if not appeared:
        log_area.warning("   都道府県モーダルの中身が生成されませんでした（タイムアウト）。")
        return False

    def open_accordion_by_text(text, level_class):
        if not text:
            return False
        target = _norm(text)
        headers = driver.find_elements(By.CSS_SELECTOR, f"button.{level_class}")
        for strict in (True, False):
            for h in headers:
                try:
                    htxt = _norm(_jstext(driver, h))
                    if not htxt:
                        continue
                    matched = (htxt == target) if strict else (target in htxt or htxt in target)
                    if matched:
                        hw_click(driver, h)
                        time.sleep(0.8)
                        return True
                except Exception:
                    continue
        return False

    # Lv2 都道府県（F列）
    if not open_accordion_by_text(pref, "ac_headerTwo"):
        try:
            names = driver.execute_script(
                "return Array.from(document.querySelectorAll('button.ac_headerTwo'))"
                ".slice(0,12).map(b => (b.textContent||'').trim());")
            log_area.warning(f"   都道府県「{pref}」が見つかりませんでした。候補例: {names}")
        except Exception:
            log_area.warning(f"   都道府県「{pref}」が見つかりませんでした。")

    # Lv3 中分類（G列）
    if mid_cat:
        if not open_accordion_by_text(mid_cat, "ac_headerThree"):
            try:
                names = driver.execute_script(
                    "return Array.from(document.querySelectorAll('button.ac_headerThree'))"
                    ".filter(b=>b.offsetParent!==null).slice(0,12).map(b => (b.textContent||'').trim());")
                log_area.warning(f"   中分類「{mid_cat}」が見つかりませんでした。候補例: {names}")
            except Exception:
                log_area.warning(f"   中分類「{mid_cat}」が見つかりませんでした。")

    # Lv4 市区町村（H〜L列、最大5つ）
    for city in cities:
        if not city:
            continue
        city_norm = _norm(city)
        checked = False
        for strict in (True, False):
            labels = driver.find_elements(By.CSS_SELECTOR, "div.modal.middle label.forLabel")
            for lb in labels:
                try:
                    ltxt = _norm(_jstext(driver, lb))
                    if not ltxt:
                        continue
                    matched = (ltxt == city_norm) if strict else (city_norm in ltxt or ltxt in city_norm)
                    if matched:
                        inp = lb.find_element(By.TAG_NAME, "input")
                        if not inp.is_selected():
                            hw_click(driver, inp)
                        checked = True
                        break
                except Exception:
                    continue
            if checked:
                break
        if not checked:
            log_area.warning(f"   市区町村「{city}」が見つかりませんでした。")
        else:
            log_area.text(f"   市区町村「{city}」を選択しました。")

    # 決定ボタン
    try:
        time.sleep(0.3)
        decided = False
        cand = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'modal')]//input[@value='決定' and not(@onclick[contains(.,'EasyShokusyu')]) and not(@onclick[contains(.,'Jyouken')])]"
            " | //div[contains(@class,'modal')]//button[contains(text(),'決定')]")
        for b in cand:
            try:
                if b.is_displayed():
                    hw_click(driver, b)
                    decided = True
                    break
            except Exception:
                continue
        if not decided:
            for b in cand:
                try:
                    hw_click(driver, b)
                    decided = True
                    break
                except Exception:
                    continue
        time.sleep(1.0)
        if not decided:
            log_area.warning("   就業場所の決定ボタンが見つかりませんでした。")
            return False
    except Exception as e:
        log_area.warning(f"   就業場所の決定ボタン押下に失敗: {e}")
        return False
    return True


def hw_select_shokusyu(driver, dai_name, sho_list, log_area):
    """
    職種：M列の大項目を開き、N/O/P列の小項目にチェックを入れて決定。
    dai_name(M列)=清掃・軽作業, sho_list(N/O/P列)=[包装・ピッキング, 洗い場（食器）, その他]
    小項目が全て空なら「こだわらない」をチェック。
    """
    if not dai_name:
        return
    nm = _norm(dai_name)
    suffix = None
    for kws, sfx in HW_SHOKUSYU_DAI:
        if any(_norm(kw) in nm for kw in kws):
            suffix = sfx
            break
    if not suffix:
        log_area.warning(f"   職種大項目「{dai_name}」に該当する分類が見つかりませんでした。")
        return

    try:
        # 大項目チェックボックスをクリックしてモーダルを開く
        dai = driver.find_element(By.CSS_SELECTOR, f"input.easyShokusyuKNo{suffix}")
        hw_click(driver, dai)
        time.sleep(0.8)

        # モーダル内スコープ
        modal_sel = f"div.modalEasyShokusyuBox{suffix}"
        targets = [s for s in sho_list if s]

        if not targets:
            # 小項目指定なし → こだわらない
            targets = ["こだわらない"]

        for sho in targets:
            sho_norm = _norm(sho)
            hit = False
            for strict in (True, False):
                labels = driver.find_elements(By.CSS_SELECTOR, f"{modal_sel} label")
                for lb in labels:
                    try:
                        ltxt = _norm(_jstext(driver, lb))
                        if not ltxt:
                            continue
                        matched = (ltxt == sho_norm) if strict else (sho_norm in ltxt or ltxt in sho_norm)
                        if matched:
                            inp = lb.find_element(By.TAG_NAME, "input")
                            if not inp.is_selected():
                                hw_click(driver, inp)
                            hit = True
                            break
                    except Exception:
                        continue
                if hit:
                    break
            if hit:
                log_area.text(f"   職種「{dai_name}」＞「{sho}」を選択しました。")
            else:
                log_area.warning(f"   職種小項目「{sho}」が見つかりませんでした。")

        time.sleep(0.3)
        # 決定（saveEasyShokusyuModal）
        for b in driver.find_elements(By.XPATH, f"//div[contains(@class,'modalEasyShokusyuBox{suffix}')]//input[@value='決定']"):
            if b.is_displayed():
                hw_click(driver, b)
                break
        time.sleep(0.5)
    except Exception as e:
        log_area.warning(f"   職種「{dai_name}」の選択に失敗: {e}")


def hw_collect_detail_urls(driver, log_area):
    """検索結果一覧から全ページをめくり、各カードの『詳細を表示』リンクを収集。"""
    detail_urls = []
    page_num = 1
    base = "https://www.hellowork.mhlw.go.jp/kensaku/"
    while True:
        time.sleep(2.0)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        if "該当する求人はありませんでした" in soup.text or "条件に一致する求人は" in soup.text:
            log_area.info("   該当求人なし。")
            break
        page_count = 0
        for a in soup.find_all('a', id="ID_dispDetailBtn", href=True):
            href = a['href']
            if "action=dispDetailBtn" in href:
                full = urllib.parse.urljoin(base, href.replace('&amp;', '&'))
                if full not in detail_urls:
                    detail_urls.append(full)
                    page_count += 1
        log_area.text(f"   {page_num}ページ目から {page_count}件 の詳細リンクを取得（累計{len(detail_urls)}件）")
        if page_count == 0:
            break
        try:
            next_btns = driver.find_elements(By.CSS_SELECTOR, "input[name='fwListNaviBtnNext']")
            target = None
            for b in next_btns:
                if b.is_displayed() and b.is_enabled():
                    target = b
                    break
            if not target:
                break
            hw_click(driver, target)
            page_num += 1
            time.sleep(2.0)
        except Exception:
            break
    return list(dict.fromkeys(detail_urls))


def hw_extract_detail(soup, dai_shokusyu):
    """
    ハローワーク詳細ページから31項目を抽出。
    確実な id は id で、それ以外はラベル（見出し）テキストから隣接値を取得する。
    dai_shokusyu: 検索条件M列の大項目（詳細ページに無いのでシート値を使う）
    戻り値: 転記順（A〜AF）の list
    """
    NA = "記載なし"

    def by_id(idname):
        el = soup.find(id=idname)
        if el:
            return re.sub(r'\s+', ' ', el.get_text(separator=" ", strip=True))
        return ""

    def by_label(labels, want_all=False):
        """
        見出しセル（th/dt/1列目td）のテキストが labels のいずれかを含む行の、
        隣（値セル）のテキストを返す。ハローワークの求人票は表形式。
        """
        results = []
        for cell in soup.find_all(['th', 'dt']):
            t = cell.get_text(strip=True)
            if not t:
                continue
            if any(lb in t for lb in labels):
                val = cell.find_next_sibling(['td', 'dd'])
                if val is None:
                    val = cell.find_next(['td', 'dd'])
                if val is not None:
                    txt = re.sub(r'\s+', ' ', val.get_text(separator=" ", strip=True))
                    if txt:
                        if not want_all:
                            return txt
                        results.append(txt)
        # thが無いテーブル向け：1列目td を見出しとみなす
        if not results:
            for tr in soup.find_all('tr'):
                tds = tr.find_all('td')
                if len(tds) >= 2:
                    head = tds[0].get_text(strip=True)
                    if any(lb in head for lb in labels):
                        txt = re.sub(r'\s+', ' ', tds[1].get_text(separator=" ", strip=True))
                        if txt:
                            if not want_all:
                                return txt
                            results.append(txt)
        if want_all:
            return " / ".join(results) if results else ""
        return ""

    def pick(idname, labels):
        """id優先、無ければラベル、どちらも無ければNA"""
        v = by_id(idname) if idname else ""
        if not v and labels:
            v = by_label(labels)
        return v if v else NA

    # 事業所名（カナのみの重複idを避ける）
    jgsh = ""
    for el in soup.find_all(id="ID_jgshMei"):
        t = re.sub(r'\s+', ' ', el.get_text(strip=True))
        if t and not re.fullmatch(r'[ァ-ヶ　ー・]+', t):
            jgsh = t
            break
    if not jgsh:
        jgsh = by_label(["事業所名"]) or NA

    # 電話番号
    phone = by_id("ID_ttsTel") or by_label(["電話番号"])
    if not phone:
        m = re.search(r'0\d{1,4}-\d{1,4}-\d{3,4}', soup.get_text())
        phone = m.group(0) if m else NA

    row = [
        "",                                                    # A
        "",                                                    # B 問い合わせ
        dai_shokusyu if dai_shokusyu else NA,                  # C 希望する職種（大項目）
        pick("ID_kjNo", ["求人番号"]),                          # D 求人番号
        pick("", ["紹介期限日"]),                               # E 紹介期限日
        jgsh,                                                  # F 事業所名
        pick("", ["所在地"]),                                   # G 所在地
        pick("", ["ホームページ", "会社のHP", "URL"]),          # H ホームページ
        pick("ID_jigyoNy", ["事業内容"]),                       # I 事業内容
        pick("", ["会社の特長", "会社の特徴"]),                  # J 会社の特長
        pick("ID_shgBsJusho", ["就業場所", "事業所住所"]),       # K 事業所住所
        pick("", ["職種"]),                                     # L 職種
        pick("ID_shigotoNy", ["仕事内容", "職務内容"]),          # M 仕事内容
        pick("", ["雇用形態"]),                                 # N 雇用形態
        pick("ID_shgBsJusho", ["就業場所"]),                    # O 就業場所
        pick("", ["最寄り駅", "交通手段", "所要時間"]),          # P 最寄り駅→選考場所
        pick("", ["マイカー通勤"]),                             # Q マイカー通勤
        pick("", ["必要な免許・資格", "免許・資格", "必要な資格"]), # R 必要な免許・資格
        pick("", ["基本給", "基本給(a)", "基本給（a）"]),        # S 基本給
        pick("", ["賞与"]),                                     # T 賞与
        pick("", ["通勤手当"]),                                 # U 通勤手当
        pick("", ["就業時間"]),                                 # V 就業時間
        pick("", ["就業時間に関する特記事項", "終業時間に関する特記事項", "時間外労働"]),  # W
        pick("", ["週所定労働日数"]),                           # X 週所定労働日数
        pick("", ["休日"]),                                     # Y 休日等
        pick("", ["選考方法"]),                                 # Z 選考方法
        pick("", ["応募書類"]),                                 # AA 応募書類等
        pick("", ["郵送", "書類の送付先", "送付場所"]),          # AB 郵送の送付場所
        pick("", ["選考に関する特記事項"]),                     # AC 選考に関する特記事項
        pick("", ["採用担当", "担当者"]),                       # AD 担当者
        phone,                                                 # AE 電話番号
        pick("", ["メール", "E-mail", "Ｅメール"]),             # AF メール
    ]
    return row


HW_HEADER = [
    "", "問い合わせ", "希望する職種（大項目）", "求人番号", "紹介期限日", "事業所名",
    "所在地", "ホームページ", "事業内容", "会社の特長", "事業所住所", "職種", "仕事内容",
    "雇用形態", "就業場所", "最寄り駅から選考場所までの交通手段・所要時間", "マイカー通勤",
    "必要な免許・資格", "基本給", "賞与", "通勤手当", "就業時間", "終業時間に関する特記事項",
    "週所定労働日数", "休日等", "選考方法", "応募書類等", "郵送の送付場所",
    "選考に関する特記事項", "担当者", "電話番号", "メール",
]


def write_to_target_sheet(gc, target_url, data, log_area):
    """Q列のURL先スプレッドシートへ書き込む。前回と同じ『RPA更新用』優先ロジック。"""
    try:
        sheet_key = target_url.split('/d/')[1].split('/')[0]
        target_ss = gc.open_by_key(sheet_key)
        target_sheet = None
        try:
            target_sheet = target_ss.worksheet("RPA更新用")
        except Exception:
            pass
        if not target_sheet:
            target_sheet = target_ss.sheet1
        log_area.text(f"   -> 転記先シート「{target_sheet.title}」に書き込みます...")
        rows_to_append = []
        if not target_sheet.get_all_values():
            rows_to_append.append(HW_HEADER)
        rows_to_append.extend(data)
        if rows_to_append:
            target_sheet.append_rows(rows_to_append)
        log_area.success(f"   データをスプレッドシートに転記しました（{len(data)}件）")
    except Exception as e:
        log_area.error(f"   転記エラー: {e}")


# ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝ 画面描画 ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
st.markdown("""
<style>
html, body, [class*="css"], .stApp, .stMarkdown, p, div, span, label, button, input, select {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
                 "Helvetica Neue", "Hiragino Sans", "Hiragino Kaku Gothic ProN",
                 "Yu Gothic", Meiryo, sans-serif !important;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}
.stApp { background: #fbfbfd; }
.block-container { padding-top: 4.5rem !important; padding-bottom: 4rem !important; max-width: 1100px; }
h1, h2, h3 { color: #1d1d1f !important; font-weight: 600 !important; letter-spacing: -0.02em !important; }
.stMarkdown p { color: #6e6e73; }
section[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid #ececec; }
section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }
.sidebar-title { font-size: 0.78rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: #86868b; margin: 0.2rem 0 0.6rem 0; }
div.stButton > button {
    background: #0071e3; color: #ffffff; border: none; border-radius: 980px;
    padding: 0.7rem 2rem; font-size: 1.02rem; font-weight: 500; letter-spacing: 0.01em;
    transition: all 0.2s ease; box-shadow: 0 1px 4px rgba(0,113,227,0.25);
}
div.stButton > button:hover { background: #0077ed; transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0,113,227,0.32); }
div.stButton > button:active { transform: translateY(0); }
hr { border-color: #ececec !important; }
div[data-testid="stAlert"] { border-radius: 14px; border: none; }
div[data-testid="stProgress"] > div > div > div > div { background-color: #0071e3; }
.asumo-header { display: flex; align-items: center; gap: 0.9rem; margin-top: 1.2rem; margin-bottom: 0.6rem; }
.asumo-header img { height: 46px; display: block; }
.asumo-sub { color: #86868b; font-size: 1.0rem; font-weight: 400; margin: 0.1rem 0 1.8rem 0; letter-spacing: 0.01em; }
.asumo-hero-title { font-size: 2.4rem; font-weight: 600; letter-spacing: -0.03em; color: #1d1d1f; margin: 1.6rem 0 0.2rem 0; }
.hw-badge { display:inline-block; background:#eef6ff; color:#0071e3; border-radius:980px; padding:0.28rem 1rem; font-size:0.9rem; font-weight:600; }
</style>
""", unsafe_allow_html=True)


def _load_logo_b64():
    for p in ("logo2.png", "logo.png", "assets/logo2.png"):
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return _b64.b64encode(f.read()).decode()
            except Exception:
                pass
    return None


_logo_b64 = _load_logo_b64()
if _logo_b64:
    st.markdown(
        f'<div class="asumo-header"><img src="data:image/png;base64,{_logo_b64}" alt="ASUMO"/></div>',
        unsafe_allow_html=True)
else:
    st.markdown('<div class="asumo-header"><span style="font-size:1.8rem;font-weight:700;letter-spacing:0.15em;color:#1d1d1f;">ASUMO</span></div>', unsafe_allow_html=True)

st.markdown('<div class="asumo-hero-title">ハローワーク求人 自動取得システム</div>', unsafe_allow_html=True)
st.markdown('<p class="asumo-sub">求人依頼シートの検索条件を読み取り、ハローワークの求人情報を自動で取得・転記します。</p>', unsafe_allow_html=True)

st.sidebar.markdown('<div class="sidebar-title">対象媒体</div>', unsafe_allow_html=True)
st.sidebar.markdown('<div style="background:#ffffff;border:1.5px solid #0071e3;border-radius:12px;padding:0.7rem 0.9rem;box-shadow:0 2px 10px rgba(0,113,227,0.12);"><span style="font-size:0.95rem;font-weight:600;color:#1d1d1f;">ハローワーク</span></div>', unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)

if st.button("取得を開始", type="primary"):
    log_area = st.container()
    with st.spinner("処理を実行中です... しばらくお待ちください。"):
        try:
            log_area.text("スプレッドシートに接続中...")
            if not os.path.exists('credentials.json'):
                st.error("GitHub上に credentials.json が見つかりません。")
                st.stop()

            gc = gspread.service_account(filename='credentials.json')
            main_ss = gc.open_by_key(MAIN_SHEET_KEY)
            sheet = main_ss.get_worksheet_by_id(MAIN_SHEET_GID)
            records = sheet.get_all_values()
            log_area.text(f"シートの読み込み完了！ 全部で {len(records)} 行あります。")

            driver = setup_browser()

            # データは5行目（index4）から。ヘッダーは3行目。
            for i, row in enumerate(records[4:], start=5):
                def gcol(n):
                    return row[n].strip() if len(row) > n and row[n] else ""

                client = gcol(1)   # B列 該当クライアント
                kubun = gcol(2)    # C列 区分
                # C列（区分）が空欄の行はスキップ
                if not kubun:
                    continue

                kinmu_jikan = gcol(3)  # D列 勤務時間（パート等）
                # E列(4) 都道府県エリア は関東等。県から導出できるため未使用でも可
                pref = gcol(5)     # F列 都道府県
                mid_cat = gcol(6)  # G列 市区町村エリア（中分類）
                cities = [gcol(7), gcol(8), gcol(9), gcol(10), gcol(11)]  # H〜L列 市区町村①〜⑤
                dai_shokusyu = gcol(12)  # M列 希望する職種（大項目）
                sho_list = [gcol(13), gcol(14), gcol(15)]  # N/O/P列 小項目①②③
                target_url = gcol(16)  # Q列 転記先URL

                if not pref:
                    log_area.warning(f"{i}行目（{client}）：都道府県が空欄のためスキップします。")
                    continue
                if not target_url:
                    log_area.warning(f"{i}行目（{client}）：転記先URL（Q列）が空欄のためスキップします。")
                    continue

                log_area.markdown(f"### 【{client}】 の取得を開始（ハローワーク）...")

                # フォーム操作
                log_area.text("   ハローワーク検索フォームを開いています...")
                driver.get(HW_SEARCH_URL)
                time.sleep(2.5)
                try:
                    hw_select_kubun(driver, kubun, kinmu_jikan, log_area)
                    hw_select_area(driver, pref, mid_cat, cities, log_area)
                    hw_select_shokusyu(driver, dai_shokusyu, sho_list, log_area)
                    time.sleep(0.5)
                    search_btn = driver.find_element(By.ID, "ID_searchBtn")
                    hw_click(driver, search_btn)
                    log_area.text("   検索を実行しました。結果一覧を取得します...")
                    time.sleep(2.5)
                except Exception as e:
                    log_area.error(f"   検索フォーム操作でエラー: {e}")
                    continue

                detail_urls = hw_collect_detail_urls(driver, log_area)
                detail_urls = list(dict.fromkeys(detail_urls))
                log_area.info(f"★ 合計 {len(detail_urls)}件 の詳細ページを抽出します。")
                if len(detail_urls) == 0:
                    continue

                scraped_data = []
                my_bar = st.progress(0, text="詳細情報を抽出中...")
                for idx, url in enumerate(detail_urls):
                    my_bar.progress((idx + 1) / len(detail_urls),
                                    text=f"詳細情報を抽出中... ({idx+1}/{len(detail_urls)}件)")
                    try:
                        driver.get(url)
                        time.sleep(1.5)
                        detail_soup = BeautifulSoup(driver.page_source, "html.parser")
                        rowdata = hw_extract_detail(detail_soup, dai_shokusyu)
                        # 1名除外なし → 全件転記
                        scraped_data.append(rowdata)
                    except Exception as e:
                        log_area.warning(f"   詳細抽出エラー（スキップ）: {e}")
                time.sleep(1)
                my_bar.empty()

                if scraped_data:
                    write_to_target_sheet(gc, target_url, scraped_data, log_area)

            driver.quit()
            log_area.success("すべての処理が完了しました！")

        except Exception as e:
            st.error(f"エラーが発生しました: {e}")
            try:
                driver.quit()
            except Exception:
                pass
