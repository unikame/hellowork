import streamlit as st
import time
import re
import os
import json
import base64 as _b64
import urllib.parse
import subprocess
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ＝＝＝ 検索条件スプレッドシート ＝＝＝
MAIN_SHEET_KEY = "1vhIfDZ1_GjLGspclN6ZvutdbWitdMO9qx6Aqhh9B4cQ"
MAIN_SHEET_GID = 1003420488  # 「求人依頼シート」

HW_SEARCH_URL = "https://www.hellowork.mhlw.go.jp/kensaku/GECA110010.do?action=initDisp&screenId=GECA110010"

# ＝＝＝ 求職番号（コードに埋め込み）＝＝＝
# 「60049-58004503」を前半5桁・後半8桁に分割
HW_KYUSHOKU_NO_JO = "60049"     # 前半5桁（ID_kSNoJo）
HW_KYUSHOKU_NO_GE = "58004503"  # 後半8桁（ID_kSNoGe）

st.set_page_config(page_title="ASUMO ハローワーク求人取得", page_icon="??",
                   layout="wide", initial_sidebar_state="expanded")


# ＝＝＝ Playwrightのブラウザを初回起動時にインストール ＝＝＝
@st.cache_resource
def ensure_playwright_browser():
    """Playwright用のChromiumをインストール（初回のみ、以降キャッシュ）"""
    try:
        subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True, timeout=300, check=False
        )
    except Exception:
        pass
    return True


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


# ＝＝＝ Playwright ブラウザ操作ヘルパー ＝＝＝
def pw_text_of(loc):
    """要素のtextContentを取得（非表示でも取れる）"""
    try:
        t = loc.text_content(timeout=2000)
        return t if t else ""
    except Exception:
        try:
            return loc.evaluate("el => el.textContent || ''")
        except Exception:
            return ""


def hw_input_kyushoku_no(page, log_area):
    """求職番号を入力する（前半5桁 ID_kSNoJo ＋ 後半8桁 ID_kSNoGe）。"""
    try:
        # 求職番号入力アコーディオンが閉じている場合は開く
        try:
            acc = page.locator("button.arrowBtn_arrowLocationKyushoku, button:has-text('求職番号入力')").first
            # 既定で開いている（is-open）ことが多いが、閉じていれば開く
            content = page.locator("div.ksno-input-default-open")
            if content.count() > 0:
                cls = content.first.get_attribute("class") or ""
                if "is-open" not in cls:
                    acc.click(force=True, timeout=5000)
                    time.sleep(0.5)
        except Exception:
            pass

        jo = page.locator("#ID_kSNoJo")
        ge = page.locator("#ID_kSNoGe")
        if jo.count() > 0 and ge.count() > 0:
            jo.fill(HW_KYUSHOKU_NO_JO, timeout=5000)
            ge.fill(HW_KYUSHOKU_NO_GE, timeout=5000)
            log_area.text(f"   求職番号 {HW_KYUSHOKU_NO_JO}-{HW_KYUSHOKU_NO_GE} を入力しました。")
        else:
            log_area.warning("   求職番号の入力欄が見つかりませんでした。")
    except Exception as e:
        log_area.warning(f"   求職番号の入力に失敗: {e}")


def hw_select_kubun(page, kubun, kinmu, log_area):
    """求人区分（C列）と勤務時間（D列＝パート/フルタイム）を設定。"""
    # 一般求人ラジオ（既定でchecked）
    try:
        radio = page.locator("#ID_kjKbnRadioBtn1")
        if radio.count() > 0 and not radio.is_checked():
            radio.check(force=True, timeout=5000)
    except Exception:
        pass
    if kinmu and "パート" in kinmu:
        try:
            part = page.locator("#ID_ippanCKBox2")
            if part.count() > 0 and not part.is_checked():
                part.check(force=True, timeout=5000)
            log_area.text("   求人区分：一般求人＋パート を選択しました。")
        except Exception as e:
            log_area.warning(f"   パートのチェックに失敗: {e}")
    elif kinmu and "フルタイム" in kinmu:
        try:
            full = page.locator("#ID_ippanCKBox1")
            if full.count() > 0 and not full.is_checked():
                full.check(force=True, timeout=5000)
            log_area.text("   求人区分：一般求人＋フルタイム を選択しました。")
        except Exception as e:
            log_area.warning(f"   フルタイムのチェックに失敗: {e}")


def hw_select_area(page, pref, mid_cat, cities, log_area):
    """就業場所モーダルで 都道府県→中分類→市区町村（最大5つ）を選択して決定。"""
    try:
        page.locator("#ID_todohukenHiddenAccoBtn").click(timeout=10000)
    except Exception as e:
        log_area.warning(f"   都道府県モーダルを開けませんでした: {e}")
        return False

    # モーダルの中身がJS生成されるまで待つ（最大10秒）
    appeared = False
    for _ in range(20):
        time.sleep(0.5)
        try:
            cnt = page.evaluate("document.querySelectorAll('button.ac_headerTwo').length")
        except Exception:
            cnt = 0
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
        # JS側でテキスト照合してクリック（Playwrightの要素取得の癖を回避）
        for strict_mode in (1, 0):
            clicked = page.evaluate(
                """(args) => {
                    const [levelClass, target, strict] = args;
                    const norm = s => (s||'').replace(/[\\s　・/／（）()]/g, '');
                    const btns = Array.from(document.querySelectorAll('button.' + levelClass));
                    for (const b of btns) {
                        const t = norm(b.textContent);
                        if (!t) continue;
                        const match = strict ? (t === target) : (t.includes(target) || target.includes(t));
                        if (match) { b.click(); return true; }
                    }
                    return false;
                }""",
                [level_class, target, strict_mode]
            )
            if clicked:
                time.sleep(0.8)
                return True
        return False

    # Lv2 都道府県
    if not open_accordion_by_text(pref, "ac_headerTwo"):
        try:
            names = page.evaluate(
                "Array.from(document.querySelectorAll('button.ac_headerTwo'))"
                ".slice(0,12).map(b => (b.textContent||'').trim())")
            log_area.warning(f"   都道府県「{pref}」が見つかりませんでした。候補例: {names}")
        except Exception:
            log_area.warning(f"   都道府県「{pref}」が見つかりませんでした。")

    # Lv3 中分類
    if mid_cat:
        if not open_accordion_by_text(mid_cat, "ac_headerThree"):
            log_area.warning(f"   中分類「{mid_cat}」が見つかりませんでした。")

    # Lv4 市区町村（最大5つ）
    for city in cities:
        if not city:
            continue
        city_norm = _norm(city)
        checked = False
        for strict_mode in (1, 0):
            checked = page.evaluate(
                """(args) => {
                    const [target, strict] = args;
                    const norm = s => (s||'').replace(/[\\s　・/／（）()]/g, '');
                    const labels = Array.from(document.querySelectorAll('div.modal.middle label.forLabel'));
                    for (const lb of labels) {
                        const t = norm(lb.textContent);
                        if (!t) continue;
                        const match = strict ? (t === target) : (t.includes(target) || target.includes(t));
                        if (match) {
                            const inp = lb.querySelector('input');
                            if (inp && !inp.checked) { inp.click(); }
                            return true;
                        }
                    }
                    return false;
                }""",
                [city_norm, strict_mode]
            )
            if checked:
                break
        if not checked:
            log_area.warning(f"   市区町村「{city}」が見つかりませんでした。")
        else:
            log_area.text(f"   市区町村「{city}」を選択しました。")

    # 決定ボタン（都道府県モーダルは動的生成。表示中モーダル内の「決定」をJSで確実に押す）
    try:
        time.sleep(0.5)
        clicked = page.evaluate(
            """() => {
                // 表示されているモーダルを探す
                const modals = Array.from(document.querySelectorAll('div.modal, div[class*="modal"]'));
                for (const modal of modals) {
                    // 画面に表示されているモーダルのみ対象
                    const style = window.getComputedStyle(modal);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    // 職種・条件モーダルは除外（都道府県モーダルの決定だけ押す）
                    const cls = modal.className || '';
                    if (cls.includes('EasyShokusyu') || cls.includes('Jyouken')) continue;
                    // モーダル内の「決定」ボタンを探す
                    const btns = Array.from(modal.querySelectorAll('input, button, a'));
                    for (const b of btns) {
                        const label = (b.value || b.textContent || '').trim();
                        if (label === '決定') {
                            b.click();
                            return true;
                        }
                    }
                }
                return false;
            }"""
        )
        if not clicked:
            # フォールバック：従来のlocator方式
            decide = page.locator(
                "//div[contains(@class,'modal')]//input[@value='決定' and not(@onclick[contains(.,'EasyShokusyu')]) and not(@onclick[contains(.,'Jyouken')])]"
            )
            for i in range(decide.count()):
                b = decide.nth(i)
                if b.is_visible():
                    b.click(force=True, timeout=5000)
                    clicked = True
                    break
        if clicked:
            log_area.text("   就業場所の「決定」を押しました。")
        else:
            log_area.warning("   就業場所の決定ボタンが見つかりませんでした。")
        time.sleep(1.0)
    except Exception as e:
        log_area.warning(f"   就業場所の決定ボタン押下に失敗: {e}")
        return False
    return True


def hw_select_shokusyu(page, dai_name, sho_list, log_area):
    """職種：M列の大項目を開き、N/O/P列の小項目にチェックを入れて決定。"""
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
        page.locator(f"input.easyShokusyuKNo{suffix}").first.click(force=True, timeout=8000)
        time.sleep(0.8)
        modal_sel = f"div.modalEasyShokusyuBox{suffix}"
        targets = [s for s in sho_list if s] or ["こだわらない"]

        for sho in targets:
            sho_norm = _norm(sho)
            hit = False
            for strict_mode in (1, 0):
                hit = page.evaluate(
                    """(args) => {
                        const [modalSel, target, strict] = args;
                        const norm = s => (s||'').replace(/[\\s　・/／（）()]/g, '');
                        const labels = Array.from(document.querySelectorAll(modalSel + ' label'));
                        for (const lb of labels) {
                            const t = norm(lb.textContent);
                            if (!t) continue;
                            const match = strict ? (t === target) : (t.includes(target) || target.includes(t));
                            if (match) {
                                const inp = lb.querySelector('input');
                                if (inp && !inp.checked) { inp.click(); }
                                return true;
                            }
                        }
                        return false;
                    }""",
                    [modal_sel, sho_norm, strict_mode]
                )
                if hit:
                    break
            if hit:
                log_area.text(f"   職種「{dai_name}」＞「{sho}」を選択しました。")
            else:
                log_area.warning(f"   職種小項目「{sho}」が見つかりませんでした。")

        time.sleep(0.3)
        # 決定
        dec = page.locator(f"//div[contains(@class,'modalEasyShokusyuBox{suffix}')]//input[@value='決定']")
        for i in range(dec.count()):
            b = dec.nth(i)
            if b.is_visible():
                b.click(force=True, timeout=5000)
                break
        time.sleep(0.5)
    except Exception as e:
        log_area.warning(f"   職種「{dai_name}」の選択に失敗: {e}")


def hw_collect_detail_urls(page, log_area):
    """検索結果一覧から全ページをめくり、各カードの詳細リンクを収集。"""
    detail_urls = []
    page_num = 1
    base = "https://www.hellowork.mhlw.go.jp/kensaku/"
    while True:
        time.sleep(2.0)
        soup = BeautifulSoup(page.content(), "html.parser")
        if "該当する求人はありませんでした" in soup.text or "条件に一致する求人は" in soup.text:
            log_area.info("   該当求人なし。")
            break
        page_count = 0
        # ID_dispDetailBtn は複数リンクで重複しがちなので、href基準で広く拾う
        for a in soup.find_all('a', href=True):
            href = a['href']
            if "action=dispDetailBtn" in href:
                full = urllib.parse.urljoin(base, href.replace('&amp;', '&'))
                if full not in detail_urls:
                    detail_urls.append(full)
                    page_count += 1
        # onclick属性に埋め込まれている場合も拾う
        if page_count == 0:
            for el in soup.find_all(attrs={"onclick": True}):
                oc = el.get("onclick", "")
                if "dispDetailBtn" in oc:
                    m = re.search(r"kJNo=(\d+)", oc)
                    if m:
                        # 検出できたことだけ記録（URL化は難しいので診断用）
                        page_count = page_count  # noop
        log_area.text(f"   {page_num}ページ目から {page_count}件 の詳細リンクを取得（累計{len(detail_urls)}件）")
        if page_count == 0:
            break
        # 「次へ＞」ボタン
        try:
            nxt = page.locator("input[name='fwListNaviBtnNext']")
            clicked = False
            for i in range(nxt.count()):
                b = nxt.nth(i)
                if b.is_visible() and b.is_enabled():
                    b.click(force=True, timeout=5000)
                    clicked = True
                    break
            if not clicked:
                break
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
    戻り値: 転記順（A?AF）の list
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
            log_area.text("Playwrightブラウザを準備中...")
            ensure_playwright_browser()

            log_area.text("スプレッドシートに接続中...")
            try:
                creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS"])
                if "private_key" in creds_dict:
                    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            except Exception as e:
                st.error(f"Secretsの読み込みに失敗しました: {e}")
                st.stop()
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            gc = gspread.authorize(creds)
            main_ss = gc.open_by_key(MAIN_SHEET_KEY)
            sheet = main_ss.get_worksheet_by_id(MAIN_SHEET_GID)
            records = sheet.get_all_values()
            log_area.text(f"シートの読み込み完了！ 全部で {len(records)} 行あります。")

            # ＝＝＝ Playwrightブラウザ起動 ＝＝＝
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-setuid-sandbox',
                        '--single-process',
                        '--no-zygote',
                    ]
                )
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1920, 'height': 1080},
                )
                page = context.new_page()
                page.set_default_timeout(30000)
                log_area.success("ブラウザの起動に成功しました！")

                # データは5行目（index4）から
                for i, row in enumerate(records[4:], start=5):
                    def gcol(n):
                        return row[n].strip() if len(row) > n and row[n] else ""

                    client = gcol(1)   # B列 該当クライアント
                    kubun = gcol(2)    # C列 区分
                    if not kubun:
                        continue

                    kinmu_jikan = gcol(3)  # D列 勤務時間
                    pref = gcol(5)     # F列 都道府県
                    mid_cat = gcol(6)  # G列 市区町村エリア
                    cities = [gcol(7), gcol(8), gcol(9), gcol(10), gcol(11)]  # H?L列
                    dai_shokusyu = gcol(12)  # M列 職種大項目
                    sho_list = [gcol(13), gcol(14), gcol(15)]  # N/O/P列
                    target_url = gcol(16)  # Q列 転記先URL

                    if not pref:
                        log_area.warning(f"{i}行目（{client}）：都道府県が空欄のためスキップします。")
                        continue
                    if not target_url:
                        log_area.warning(f"{i}行目（{client}）：転記先URL（Q列）が空欄のためスキップします。")
                        continue

                    log_area.markdown(f"### 【{client}】 の取得を開始（ハローワーク）...")
                    log_area.text("   ハローワーク検索フォームを開いています...")
                    page.goto(HW_SEARCH_URL, wait_until="domcontentloaded")
                    time.sleep(2.5)

                    try:
                        hw_input_kyushoku_no(page, log_area)
                        hw_select_kubun(page, kubun, kinmu_jikan, log_area)
                        hw_select_area(page, pref, mid_cat, cities, log_area)
                        hw_select_shokusyu(page, dai_shokusyu, sho_list, log_area)
                        time.sleep(1.0)

                        # 【診断】検索ボタンを押す直前のスクリーンショット
                        try:
                            shot_before = page.screenshot(full_page=True)
                            st.image(shot_before, caption="検索ボタンを押す直前の画面", use_container_width=True)
                        except Exception as e:
                            log_area.warning(f"   直前スクショ失敗: {e}")

                        # 「検索する」ボタン（ID_searchBtn）をクリック
                        try:
                            page.locator("#ID_searchBtn").first.scroll_into_view_if_needed(timeout=5000)
                            page.locator("#ID_searchBtn").first.click(timeout=10000)
                        except Exception:
                            try:
                                page.evaluate("document.querySelector('#ID_searchBtn').click()")
                            except Exception:
                                page.evaluate("document.querySelector('#ID_searchBtn').closest('form').submit()")
                        log_area.text("   検索を実行しました。結果ページへの遷移を待っています...")

                        # 検索結果（○件中 1〜）が現れるまで待つ（最大40秒）
                        transitioned = False
                        for _ in range(40):
                            time.sleep(1.0)
                            try:
                                content = page.content()
                            except Exception:
                                content = ""
                            if re.search(r'\d[\d,]*\s*件中\s*\d', content) or "該当する求人はありませんでした" in content:
                                transitioned = True
                                break
                        time.sleep(1.5)

                        # 【診断】検索ボタンを押した後のスクリーンショット
                        try:
                            shot_after = page.screenshot(full_page=True)
                            st.image(shot_after, caption="検索ボタンを押した後の画面", use_container_width=True)
                        except Exception as e:
                            log_area.warning(f"   直後スクショ失敗: {e}")

                        # ＝＝＝ 遷移後の診断 ＝＝＝
                        cur_url = page.url
                        soup_diag = BeautifulSoup(page.content(), "html.parser")
                        ken = ""
                        m = re.search(r'(\d[\d,]*)\s*件中', soup_diag.get_text())
                        if m:
                            ken = m.group(0)
                        body_text = soup_diag.get_text()
                        err_msg = ""
                        for kw in ["エラー", "選択してください", "入力してください", "該当する求人はありませんでした"]:
                            if kw in body_text:
                                idx = body_text.find(kw)
                                err_msg = body_text[max(0, idx-20):idx+40].strip()
                                break
                        status = "結果ページに遷移OK" if transitioned else "★遷移せず（フォームのまま）"
                        log_area.info(f"   {status}\n   遷移先URL: {cur_url}\n   該当件数表示: {ken or '見つからず'}\n   注目メッセージ: {err_msg or 'なし'}")
                    except Exception as e:
                        log_area.error(f"   検索フォーム操作でエラー: {e}")
                        continue

                    detail_urls = hw_collect_detail_urls(page, log_area)
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
                            page.goto(url, wait_until="domcontentloaded")
                            time.sleep(1.5)
                            detail_soup = BeautifulSoup(page.content(), "html.parser")
                            rowdata = hw_extract_detail(detail_soup, dai_shokusyu)
                            scraped_data.append(rowdata)
                        except Exception as e:
                            log_area.warning(f"   詳細抽出エラー（スキップ）: {e}")
                    time.sleep(1)
                    my_bar.empty()

                    if scraped_data:
                        write_to_target_sheet(gc, target_url, scraped_data, log_area)

                browser.close()

            log_area.success("すべての処理が完了しました！")

        except Exception as e:
            st.error(f"エラーが発生しました: {e}")
