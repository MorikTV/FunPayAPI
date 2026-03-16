from __future__ import annotations

import html
import json
import logging
import random
import re
import string
import time
from typing import TYPE_CHECKING, Any, IO, Literal, Optional

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from requests_toolbelt import MultipartEncoder
from urllib3.util.retry import Retry

from .common import enums
from .common.utils import parse_currency, RegularExpressions
from . import types
from .common import exceptions, utils
from .types import PaymentMethod, CalcResult

if TYPE_CHECKING:
    from .updater.runner import Runner

logger = logging.getLogger("FunPayAPI.account")
PRIVATE_CHAT_ID_RE = re.compile(r"users-\d+-\d+$")


class Account:
    """
    Класс для управления аккаунтом FunPay.

    :param golden_key: токен (golden_key) аккаунта.
    :param user_agent: user-agent браузера, с которого был произведен вход.
    :param requests_timeout: тайм-аут ожидания ответа (секунды).
    :param proxy: прокси для запросов {``"https"``: ``"..."``}.
    :param locale: язык аккаунта по умолчанию (``"ru"``, ``"en"`` или ``"uk"``).
    """

    def __init__(self, golden_key: str,
                 user_agent: str | None = None,
                 requests_timeout: int | float = 10,
                 proxy: Optional[dict] = None,
                 locale: Literal["ru", "en", "uk"] | None = None):
        self.golden_key: str = golden_key
        self.user_agent: str | None = user_agent
        self.requests_timeout: int | float = requests_timeout
        self.proxy: Optional[dict] = proxy

        # ── Данные аккаунта (заполняются после Account.get()) ────────────────
        self.html: str | None = None
        self.app_data: dict | None = None
        self.id: int | None = None
        self.username: str | None = None
        self.active_sales: int | None = None
        self.active_purchases: int | None = None
        self.csrf_token: str | None = None
        self.phpsessid: str | None = None
        self.last_update: int | None = None
        self.currency: types.Currency = types.Currency.UNKNOWN
        self.total_balance: int | None = None
        self._logout_link: str | None = None

        # ── Отслеживание rate-limit ошибок ───────────────────────────────────
        self.last_429_err_time: float = 0
        self.last_flood_err_time: float = 0
        self.last_multiuser_flood_err_time: float = 0

        # ── Локали ───────────────────────────────────────────────────────────
        self.__locale: Literal["ru", "en", "uk"] | None = None
        self.__default_locale: Literal["ru", "en", "uk"] | None = locale
        self.__profile_parse_locale: Literal["ru", "en", "uk"] | None = locale
        self.__chat_parse_locale: Literal["ru", "en", "uk"] | None = None
        self.__order_parse_locale: Literal["ru", "en", "uk"] | None = None
        self.__lots_parse_locale: Literal["ru", "en", "uk"] | None = None
        self.__subcategories_parse_locale: Literal["ru", "en", "uk"] | None = None
        self.__set_locale: Literal["ru", "en", "uk"] | None = None

        # ── Внутреннее состояние ─────────────────────────────────────────────
        self.__initiated: bool = False
        self.__saved_chats: dict[int, types.ChatShortcut] = {}
        self.runner: Runner | None = None

        self.__categories: list[types.Category] = []
        self.__sorted_categories: dict[int, types.Category] = {}
        self.__subcategories: list[types.SubCategory] = []
        self.__sorted_subcategories: dict[types.SubCategoryTypes, dict[int, types.SubCategory]] = {
            types.SubCategoryTypes.COMMON: {},
            types.SubCategoryTypes.CURRENCY: {},
        }

        # ── Маркеры bot-сообщений ─────────────────────────────────────────────
        self.__bot_character: str = "⁡"
        self.__old_bot_character: str = "⁤"

        # ── HTTP сессия с авто-retry ─────────────────────────────────────────
        self.session = requests.Session()
        retry_strategy = Retry(
            total=6, connect=6, read=6, redirect=6, status=6,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods={"GET", "POST"},
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)

    # ══════════════════════════════════════════════════════════════════════════
    # Свойства
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def bot_character(self) -> str:
        return self.__bot_character

    @property
    def old_bot_character(self) -> str:
        return self.__old_bot_character

    @property
    def locale(self) -> Literal["ru", "en", "uk"] | None:
        return self.__locale

    @locale.setter
    def locale(self, new_locale: Literal["ru", "en", "uk"] | None):
        if new_locale and self.__locale != new_locale and new_locale in ("ru", "en", "uk"):
            self.__set_locale = new_locale

    @property
    def is_initiated(self) -> bool:
        """Инициализирован ли аккаунт через :meth:`get`."""
        return self.__initiated

    # ══════════════════════════════════════════════════════════════════════════
    # Вспомогательные методы URL
    # ══════════════════════════════════════════════════════════════════════════

    def normalize_url(self, api_method: str,
                      locale: Literal["ru", "en", "uk"] | None = None) -> str:
        """Нормализует URL: добавляет домен и локаль при необходимости."""
        api_method = api_method.lstrip("/")
        if api_method.startswith("api/"):
            return f"https://funpay.com/{api_method}"
        if "funpay.com/api/" in api_method:
            return api_method
        api_method = "https://funpay.com/" if api_method == "https://funpay.com" else api_method
        url = (api_method if api_method.startswith("https://funpay.com/")
               else "https://funpay.com/" + api_method)
        for loc in ("en", "uk"):
            url = url.replace(f"https://funpay.com/{loc}/", "https://funpay.com/", 1)
        if not locale:
            locale = self.locale
        if locale in ("en", "uk"):
            return url.replace("https://funpay.com/", f"https://funpay.com/{locale}/", 1)
        return url

    @staticmethod
    def is_funpay_api_method(api_method: str) -> bool:
        """Является ли переданная строка вызовом нового JSON-API (/api/)."""
        return "funpay.com/api/" in api_method or api_method.startswith("api/")

    @staticmethod
    def chat_id_private(chat_id: int | str) -> bool:
        """Является ли chat_id приватным чатом (int или «users-X-Y»)."""
        return isinstance(chat_id, int) or bool(PRIVATE_CHAT_ID_RE.fullmatch(str(chat_id)))

    # ══════════════════════════════════════════════════════════════════════════
    # Низкоуровневый HTTP
    # ══════════════════════════════════════════════════════════════════════════

    def method(self, request_method: Literal["post", "get"],
               api_method: str, headers: dict, payload: Any,
               exclude_phpsessid: bool = False, raise_not_200: bool = False,
               locale: Literal["ru", "en", "uk"] | None = None) -> requests.Response:
        """
        Отправляет запрос к FunPay с автоматическим добавлением кук и user-agent.
        Поддерживает обработку 429-ошибок с экспоненциальной задержкой.
        Корректно следует редиректам FunPay, обновляя текущую локаль.

        :param request_method: ``"get"`` или ``"post"``.
        :param api_method: endpoint или полная ссылка.
        :param headers: дополнительные заголовки запроса.
        :param payload: тело запроса (dict или строка).
        :param exclude_phpsessid: исключить PHPSESSID из кук.
        :param raise_not_200: бросить исключение при статус-коде != 200.
        :param locale: принудительная локаль для этого запроса.
        :return: объект ответа.
        """
        def update_locale(redirect_url: str):
            for loc in ("en", "uk"):
                if redirect_url.startswith(f"https://funpay.com/{loc}/"):
                    self.__locale = loc
                    return
            if redirect_url.startswith("https://funpay.com"):
                self.__locale = "ru"

        if self.is_funpay_api_method(api_method):
            cookies = {"golden_key": self.golden_key}
            if self.phpsessid and not exclude_phpsessid:
                cookies["PHPSESSID"] = self.phpsessid
            link = self.normalize_url(api_method, locale)
        else:
            cookies = {"golden_key": self.golden_key, "cookie_prefs": "1"}
            if self.phpsessid and not exclude_phpsessid:
                cookies["PHPSESSID"] = self.phpsessid
            if self.user_agent:
                headers["user-agent"] = self.user_agent
            if request_method == "post" and locale:
                link = self.normalize_url(api_method, locale)
            else:
                link = self.normalize_url(api_method)
            effective_locale = locale or self.__set_locale
            if request_method == "get" and effective_locale and effective_locale != self.locale:
                link += f'{"&" if "?" in link else "?"}setlocale={effective_locale}'

        kwargs = {
            "method": request_method,
            "headers": headers,
            "timeout": self.requests_timeout,
            "proxies": self.proxy or {},
            "cookies": cookies,
        }

        i = 0
        response = None
        while i < 10:
            i += 1
            response = self.session.request(url=link, data=payload, allow_redirects=False, **kwargs)
            if response.status_code == 429:
                self.last_429_err_time = time.time()
                time.sleep(min(2 ** i, 30))
                continue
            if 300 <= response.status_code < 400 and "Location" in response.headers:
                link = response.headers["Location"]
                if link.endswith("account/login"):
                    raise exceptions.UnauthorizedError(response)
                update_locale(link)
                continue
            break
        else:
            response = self.session.request(url=link, data=payload, allow_redirects=True, **kwargs)

        if response.status_code == 403:
            raise exceptions.UnauthorizedError(response)
        if response.status_code != 200 and raise_not_200:
            raise exceptions.RequestFailedError(response)
        return response

    def runner_request(self, payload: dict) -> requests.Response:
        """
        Отправляет запрос к эндпоинту ``runner/``.

        :param payload: словарь с данными (``objects`` и ``request``).
        :return: объект ответа.
        """
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        payload["csrf_token"] = self.csrf_token
        payload["objects"] = json.dumps(payload.get("objects", []))
        payload["request"] = (False if not payload.get("request")
                              else json.dumps(payload["request"]))
        return self.method("post", "runner/", headers, payload, raise_not_200=True)

    def get_payload_data(self,
                         chats_data: dict[int | str, str | None] | list[int | str] | None = None,
                         last_order_event_tag: str | None = None,
                         last_msg_event_tag: str | None = None,
                         buyer_viewing_ids: list[int | str] | None = None,
                         request: dict | None = None,
                         include_runner_context: bool = False) -> dict:
        """
        Формирует словарь payload для запроса к runner/.

        :param chats_data: ID чатов → никнейм собеседника (None = неизвестен).
        :param last_order_event_tag: тег для отслеживания заказов.
        :param last_msg_event_tag: тег для отслеживания чатов.
        :param buyer_viewing_ids: ID покупателей для получения «Покупатель смотрит».
        :param request: дополнительный объект запроса (отправка сообщений).
        :param include_runner_context: использовать кэш Runner'а (теги, ID).
        """
        objects = []
        if chats_data:
            if include_runner_context and self.runner:
                tags = self.runner.chat_node_tags
                msg_ids = self.runner.last_messages_ids
                users_ids = self.runner.users_ids
            else:
                tags, msg_ids, users_ids = {}, {}, {}

            for chat_id in chats_data:
                literal_chat_id = None
                if chat_id in users_ids:
                    user_id = users_ids[chat_id]
                    id1, id2 = sorted([self.id, user_id])
                    literal_chat_id = f"users-{id1}-{id2}"
                objects.append({
                    "type": "chat_node",
                    "id": literal_chat_id or chat_id,
                    "tag": tags.get(chat_id) or "00000000",
                    "data": {
                        "node": literal_chat_id or chat_id,
                        "last_message": msg_ids.get(chat_id) or -1,
                        "content": "",
                    },
                })

        if last_msg_event_tag:
            objects.append({
                "type": "chat_bookmarks",
                "id": self.id,
                "tag": last_msg_event_tag,
                "data": False,
            })
        if last_order_event_tag:
            objects.append({
                "type": "orders_counters",
                "id": self.id,
                "tag": last_order_event_tag,
                "data": False,
            })
        if buyer_viewing_ids:
            objects.extend([
                {"type": "c-p-u", "id": str(i), "tag": "00000000", "data": False}
                for i in buyer_viewing_ids
            ])
        return {"objects": objects, "request": request}

    def abuse_runner(self,
                     chats_data: dict[int | str, str | None] | None = None,
                     last_order_event_tag: str | None = None,
                     last_msg_event_tag: str | None = None,
                     buyer_viewing_ids: list[int | str] | None = None,
                     request: dict | None = None,
                     include_runner_context: bool = False) -> requests.Response:
        """
        Формирует payload и отправляет через Runner (или напрямую, если Runner не привязан).

        .. warning::
            В ответе могут присутствовать данные, полученные для других запросов Runner'а.
        """
        payload_data = self.get_payload_data(
            chats_data, last_order_event_tag, last_msg_event_tag,
            buyer_viewing_ids, request, include_runner_context,
        )
        return self.runner_request(payload_data)

    # ══════════════════════════════════════════════════════════════════════════
    # Инициализация аккаунта
    # ══════════════════════════════════════════════════════════════════════════

    def get(self, update_phpsessid: bool = False) -> Account:
        """
        Получает/обновляет данные аккаунта с главной страницы FunPay.
        Необходимо вызывать каждые 40–60 минут для обновления PHPSESSID.

        :param update_phpsessid: принудительно обновить PHPSESSID.
        :return: self (для цепочки вызовов).
        """
        if not self.is_initiated:
            self.locale = self.__subcategories_parse_locale
        response = self.method("get", "https://funpay.com/", {}, {},
                               update_phpsessid, raise_not_200=True)
        if not self.is_initiated:
            self.locale = self.__default_locale

        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")

        username_div = parser.find("div", {"class": "user-link-name"})
        if not username_div:
            raise exceptions.UnauthorizedError(response)

        self.username = username_div.text
        self.app_data = json.loads(parser.find("body").get("data-app-data"))
        self.__locale = self.app_data.get("locale")
        self.id = self.app_data["userId"]
        self.csrf_token = self.app_data["csrf-token"]

        logout_a = parser.find("a", class_="menu-item-logout")
        if logout_a:
            self._logout_link = logout_a.get("href")

        active_sales = parser.find("span", {"class": "badge badge-trade"})
        self.active_sales = int(active_sales.text) if active_sales else 0

        active_purchases = parser.find("span", {"class": "badge badge-orders"})
        self.active_purchases = int(active_purchases.text) if active_purchases else 0

        balance = parser.find("span", class_="badge badge-balance")
        if balance:
            bal_text, currency = balance.text.rsplit(" ", maxsplit=1)
            self.total_balance = int(bal_text.replace(" ", ""))
            self.currency = parse_currency(currency)
        else:
            self.total_balance = 0

        cookies = response.cookies.get_dict()
        if update_phpsessid or not self.phpsessid:
            self.phpsessid = cookies.get("PHPSESSID", self.phpsessid)

        if not self.is_initiated:
            self.__setup_categories(html_response)

        self.last_update = int(time.time())
        self.html = html_response
        self.__initiated = True
        return self

    # ══════════════════════════════════════════════════════════════════════════
    # Лоты
    # ══════════════════════════════════════════════════════════════════════════

    def get_subcategory_public_lots(self, subcategory_type: enums.SubCategoryTypes,
                                    subcategory_id: int,
                                    locale: Literal["ru", "en", "uk"] | None = None
                                    ) -> list[types.LotShortcut]:
        """
        Получает список всех опубликованных лотов подкатегории.

        :param subcategory_type: тип подкатегории.
        :param subcategory_id: ID подкатегории.
        :param locale: принудительная локаль.
        :return: список лотов.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        meth = (f"lots/{subcategory_id}/" if subcategory_type is enums.SubCategoryTypes.COMMON
                else f"chips/{subcategory_id}/")
        if not locale:
            locale = self.__lots_parse_locale
        response = self.method("get", meth, {"accept": "*/*"}, {}, raise_not_200=True, locale=locale)
        if locale:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")

        if not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)
        self.__update_csrf_token(parser)

        offers = parser.find_all("a", {"class": "tc-item"})
        if not offers:
            return []

        subcategory_obj = self.get_subcategory(subcategory_type, subcategory_id)
        result, sellers, currency = [], {}, None
        for offer in offers:
            offer_id = offer["href"].split("id=")[1]
            promo = "offer-promo" in offer.get("class", [])
            description = offer.find("div", {"class": "tc-desc-text"})
            description = description.text if description else None
            server = offer.find("div", class_="tc-server")
            server = server.text if server else None
            side = offer.find("div", class_="tc-side")
            side = side.text if side else None
            tc_price = offer.find("div", {"class": "tc-price"})
            if subcategory_type is types.SubCategoryTypes.COMMON:
                price = float(tc_price["data-s"])
            else:
                price = float(tc_price.find("div").text.rsplit(maxsplit=1)[0].replace(" ", ""))
            if currency is None:
                currency = parse_currency(tc_price.find("span", class_="unit").text)
                if self.currency != currency:
                    self.currency = currency
            seller_soup = offer.find("div", class_="tc-user")
            attributes = {k.replace("data-", "", 1): (int(v) if v.isdigit() else v)
                          for k, v in offer.attrs.items() if k.startswith("data-")}
            auto = attributes.get("auto") == 1
            tc_amount = offer.find("div", class_="tc-amount")
            amount = tc_amount.text.replace(" ", "") if tc_amount else None
            amount = int(amount) if amount and amount.isdigit() else None
            seller_key = str(seller_soup)
            if seller_key not in sellers:
                online = attributes.get("online") == 1
                seller_body = offer.find("div", class_="media-body")
                uname = seller_body.find("div", class_="media-user-name").text.strip()
                rating_stars = seller_body.find("div", class_="rating-stars")
                stars = len(rating_stars.find_all("i", class_="fas")) if rating_stars else None
                k_reviews = seller_body.find("div", class_="media-user-reviews")
                k_reviews = int("".join([c for c in k_reviews.text if c.isdigit()])) if k_reviews else 0
                user_id = int(seller_body.find("span", class_="pseudo-a")["data-href"].split("/")[-2])
                seller = types.SellerShortcut(user_id, uname, online, stars, k_reviews, seller_key)
                sellers[seller_key] = seller
            else:
                seller = sellers[seller_key]
            for k in ("online", "auto"):
                attributes.pop(k, None)
            lot_obj = types.LotShortcut(offer_id, server, side, description, amount, price,
                                        currency, subcategory_obj, seller, auto, promo, attributes, str(offer))
            result.append(lot_obj)
        return result

    def get_my_subcategory_lots(self, subcategory_id: int,
                                locale: Literal["ru", "en", "uk"] | None = None
                                ) -> list[types.MyLotShortcut]:
        """
        Получает список собственных лотов подкатегории (страница /trade).

        :param subcategory_id: ID подкатегории.
        :param locale: принудительная локаль.
        :return: список лотов.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if not locale:
            locale = self.__lots_parse_locale
        response = self.method("get", f"lots/{subcategory_id}/trade",
                               {"accept": "*/*"}, {}, raise_not_200=True, locale=locale)
        if locale:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        if not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)
        self.__update_csrf_token(parser)

        offers = parser.find_all("a", class_="tc-item")
        if not offers:
            return []

        subcategory_obj = self.get_subcategory(enums.SubCategoryTypes.COMMON, subcategory_id)
        result, currency = [], None
        for offer in offers:
            offer_id = offer["data-offer"]
            description = offer.find("div", {"class": "tc-desc-text"})
            description = description.text if description else None
            server = offer.find("div", class_="tc-server")
            server = server.text if server else None
            side = offer.find("div", class_="tc-side")
            side = side.text if side else None
            tc_price = offer.find("div", class_="tc-price")
            price = float(tc_price["data-s"])
            if currency is None:
                currency = parse_currency(tc_price.find("span", class_="unit").text)
                if self.currency != currency:
                    self.currency = currency
            auto = bool(tc_price.find("i", class_="auto-dlv-icon"))
            tc_amount = offer.find("div", class_="tc-amount")
            amount = tc_amount.text.replace(" ", "") if tc_amount else None
            amount = int(amount) if amount and amount.isdigit() else None
            active = "warning" not in offer.get("class", [])
            result.append(types.MyLotShortcut(offer_id, server, side, description, amount,
                                              price, currency, subcategory_obj, auto, active, str(offer)))
        return result

    def get_lot_page(self, lot_id: int,
                     locale: Literal["ru", "en", "uk"] | None = None) -> types.LotPage | None:
        """
        Получает публичную страницу лота.

        :param lot_id: ID лота.
        :param locale: принудительная локаль.
        :return: объект страницы лота или None если лот не найден.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        response = self.method("get", f"lots/offer?id={lot_id}",
                               {"accept": "*/*"}, {}, raise_not_200=True, locale=locale)
        if locale:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        if not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)
        self.__update_csrf_token(parser)

        page_header = parser.find("h1", class_="page-header")
        if page_header and page_header.text in ("Предложение не найдено", "Пропозицію не знайдено", "Offer not found"):
            return None

        subcategory_id = int(parser.find("a", class_="js-back-link")["href"].split("/")[-2])
        chat_header = parser.find("div", class_="chat-header")
        if chat_header:
            seller = chat_header.find("div", class_="media-user-name").find("a")
            seller_id = int(seller["href"].split("/")[-2])
            seller_username = seller.text
        else:
            seller_id = self.id
            seller_username = self.username

        short_description = full_description = None
        image_urls = []
        for param_item in parser.find_all("div", class_="param-item"):
            param_name = param_item.find("h5")
            if not param_name:
                continue
            if param_name.text in ("Краткое описание", "Короткий опис", "Short description"):
                short_description = param_item.find("div").text
            elif param_name.text in ("Подробное описание", "Докладний опис", "Detailed description"):
                full_description = param_item.find("div").text
            elif param_name.text in ("Картинки", "Зображення", "Images"):
                photos = param_item.find_all("a", class_="attachments-thumb")
                image_urls = [photo.get("href") for photo in photos] if photos else []

        return types.LotPage(lot_id,
                             self.get_subcategory(enums.SubCategoryTypes.COMMON, subcategory_id),
                             short_description, full_description, image_urls,
                             seller_id, seller_username)

    def get_lot_fields(self, lot_id: int) -> types.LotFields:
        """
        Получает редактируемые поля лота (страница /lots/offerEdit).

        :param lot_id: ID лота.
        :return: объект с полями лота.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        response = self.method("get", f"lots/offerEdit?offer={lot_id}", {}, {}, raise_not_200=True)
        html_response = response.content.decode()
        bs = BeautifulSoup(html_response, "lxml")
        error_message = bs.find("p", class_="lead")
        if error_message:
            raise exceptions.LotParsingError(response, error_message.text, lot_id)
        form = bs.find("form", class_="form-offer-editor")
        result = {}
        result.update({field["name"]: field.get("value") or ""
                       for field in form.find_all("input") if field["name"] != "query"})
        result.update({field["name"]: field.text or "" for field in form.find_all("textarea")})
        result.update({
            field["name"]: field.find("option", selected=True)["value"]
            for field in form.find_all("select")
            if "hidden" not in field.find_parent(class_="form-group").get("class", [])
        })
        result.update({field["name"]: "on"
                       for field in form.find_all("input", {"type": "checkbox"}, checked=True)})
        subcategory = self.get_subcategory(enums.SubCategoryTypes.COMMON, int(result.get("node_id", 0)))
        self.csrf_token = result.get("csrf_token") or self.csrf_token
        currency = utils.parse_currency(form.find("span", class_="form-control-feedback").text)
        if self.currency != currency:
            self.currency = currency

        # Парсим таблицу платёжных методов
        buyers_prices_rows = form.find("table", class_="table-buyers-prices").find_all("tr")
        payment_methods = []
        for i, pm in enumerate(buyers_prices_rows):
            pm_price_text, pm_cur = pm.find("td").text.rsplit(maxsplit=1)
            pm_price = float(pm_price_text.replace(" ", ""))
            payment_methods.append(PaymentMethod(pm.find("th").text, pm_price,
                                                  parse_currency(pm_cur), i))
        calc_result = CalcResult(types.SubCategoryTypes.COMMON, subcategory.id if subcategory else 0,
                                 payment_methods, float(result.get("price") or 0),
                                 None, types.Currency.UNKNOWN, currency)
        db_amount = json.loads(html.unescape(form.get("data-offer", "{}"))).get("amount")
        return types.LotFields(lot_id, result, subcategory, currency, calc_result, db_amount)

    def get_chip_fields(self, subcategory_id: int) -> types.ChipFields:
        """
        Получает редактируемые поля лота-валюты.

        :param subcategory_id: ID подкатегории chips.
        :return: объект с полями.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        response = self.method("get", f"chips/{subcategory_id}/trade", {}, {}, raise_not_200=True)
        html_response = response.content.decode()
        bs = BeautifulSoup(html_response, "lxml")
        result = {field["name"]: field.get("value") or ""
                  for field in bs.find_all("input") if field["name"] != "query"}
        result.update({field["name"]: "on"
                       for field in bs.find_all("input", {"type": "checkbox"}, checked=True)})
        return types.ChipFields(self.id, subcategory_id, result)

    def save_offer(self, offer_fields: types.LotFields | types.ChipFields,
                   locale: Literal["ru", "en", "uk"] | None = None):
        """
        Сохраняет лот (обычный или chips) на FunPay.

        :param offer_fields: объект с полями лота.
        :param locale: принудительная локаль запроса.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        offer_fields.csrf_token = self.csrf_token
        fields = offer_fields.renew_fields().fields

        if isinstance(offer_fields, types.LotFields):
            id_ = offer_fields.lot_id
            api_method = "lots/offerSave"
        else:
            id_ = offer_fields.subcategory_id
            api_method = "chips/saveOffers"

        response = self.method("post", api_method, headers, fields,
                               raise_not_200=True, locale=locale)
        json_response = response.json()
        errors_dict = {}
        if (errors := json_response.get("errors")) or json_response.get("error"):
            if errors:
                for k, v in errors:
                    errors_dict[k] = v
            raise exceptions.LotSavingError(response, json_response.get("error"), id_, errors_dict)

    def save_lot(self, lot_fields: types.LotFields,
                 locale: Literal["ru", "en", "uk"] | None = None):
        """Псевдоним для :meth:`save_offer` для обычных лотов."""
        self.save_offer(lot_fields, locale)

    def save_chip(self, chip_fields: types.ChipFields,
                  locale: Literal["ru", "en", "uk"] | None = None):
        """Псевдоним для :meth:`save_offer` для лотов-валют."""
        self.save_offer(chip_fields, locale)

    def delete_lot(self, lot_id: int) -> None:
        """Удаляет лот по ID."""
        self.save_lot(types.LotFields(lot_id, {
            "csrf_token": self.csrf_token,
            "offer_id": lot_id,
            "deleted": "1",
        }))

    def raise_lots(self, category_id: int,
                   subcategories: Optional[list[int | types.SubCategory]] = None,
                   exclude: list[int] | None = None) -> bool:
        """
        Поднимает лоты всех (или указанных) подкатегорий переданной категории.

        :param category_id: ID категории (игры).
        :param subcategories: подкатегории для поднятия (все, если не указаны).
        :param exclude: ID подкатегорий, которые НЕ нужно поднимать.
        :return: True при успехе.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        category = self.get_category(category_id)
        if not category:
            raise ValueError(f"Категория {category_id} не найдена.")
        exclude = exclude or []
        if subcategories:
            subcats = []
            for sc in subcategories:
                if isinstance(sc, types.SubCategory):
                    if sc.type is types.SubCategoryTypes.COMMON and sc.category.id == category.id and sc.id not in exclude:
                        subcats.append(sc)
                else:
                    subcat = category.get_subcategory(types.SubCategoryTypes.COMMON, sc)
                    if subcat:
                        subcats.append(subcat)
        else:
            subcats = [sc for sc in category.get_subcategories()
                       if sc.type is types.SubCategoryTypes.COMMON and sc.id not in exclude]

        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        payload = {
            "game_id": category_id,
            "node_id": subcats[0].id,
            "node_ids[]": [sc.id for sc in subcats],
        }
        response = self.method("post", "lots/raise", headers, payload, raise_not_200=True)
        json_response = response.json()
        if json_response.get("error"):
            wait_time = utils.parse_wait_time(json_response.get("msg", ""))
            raise exceptions.RaiseError(response, category, json_response.get("msg"), wait_time)
        return True

    def calc(self, subcategory_type: enums.SubCategoryTypes,
             subcategory_id: int | None = None,
             game_id: int | None = None,
             price: int | float = 1000) -> types.CalcResult:
        """
        Рассчитывает комиссию FunPay для подкатегории.

        :param subcategory_type: тип подкатегории.
        :param subcategory_id: ID подкатегории (для COMMON).
        :param game_id: ID игры (для CURRENCY / chips).
        :param price: цена для расчёта.
        :return: объект с результатами расчёта.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if subcategory_type == types.SubCategoryTypes.COMMON:
            key, type_, value = "nodeId", "lots", subcategory_id
        else:
            key, type_, value = "game", "chips", game_id
        if value is None:
            raise ValueError("Необходимо указать subcategory_id или game_id.")
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        r = self.method("post", f"{type_}/calc", headers, {key: value, "price": price},
                        raise_not_200=True)
        json_resp = r.json()
        if json_resp.get("error"):
            raise exceptions.RequestFailedError(r)
        methods = [
            PaymentMethod(m.get("name"), float(m["price"].replace(" ", "")),
                          parse_currency(m.get("unit")), m.get("sort"))
            for m in json_resp.get("methods", [])
        ]
        if "minPrice" in json_resp:
            mp_text, mp_cur = json_resp["minPrice"].rsplit(" ", maxsplit=1)
            min_price = float(mp_text.replace(" ", ""))
            min_price_currency = parse_currency(mp_cur)
        else:
            min_price, min_price_currency = None, types.Currency.UNKNOWN
        return CalcResult(subcategory_type, subcategory_id, methods, price,
                          min_price, min_price_currency, self.currency)

    # ══════════════════════════════════════════════════════════════════════════
    # Баланс
    # ══════════════════════════════════════════════════════════════════════════

    def get_balance(self, lot_id: int) -> types.Balance:
        """
        Получает детальный баланс аккаунта со страницы лота.

        :param lot_id: ID любого активного лота.
        :return: объект баланса.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        response = self.method("get", f"lots/offer?id={lot_id}",
                               {"accept": "*/*"}, {}, raise_not_200=True)
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        if not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)
        self.__update_csrf_token(parser)
        balances = parser.find("select", {"name": "method"})
        return types.Balance(
            float(balances["data-balance-total-rub"]),
            float(balances["data-balance-rub"]),
            float(balances["data-balance-total-usd"]),
            float(balances["data-balance-usd"]),
            float(balances["data-balance-total-eur"]),
            float(balances["data-balance-eur"]),
        )

    def get_exchange_rate(self, currency: types.Currency) -> tuple[float, types.Currency]:
        """
        Возвращает курс обмена валюты аккаунта на указанную.

        :param currency: целевая валюта.
        :return: (коэффициент, текущая валюта аккаунта).
        """
        r = self.method(
            "post", "https://funpay.com/account/switchCurrency",
            {"accept": "*/*",
             "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
             "x-requested-with": "XMLHttpRequest"},
            {"cy": currency.code, "csrf_token": self.csrf_token, "confirmed": "false"},
            raise_not_200=True,
        )
        b = json.loads(r.text)
        if "url" in b and not b["url"]:
            self.currency = currency
            return 1.0, currency
        s = (BeautifulSoup(b["modal"], "lxml")
             .find("p", class_="lead").text.replace("\xa0", " "))
        match = RegularExpressions().EXCHANGE_RATE.fullmatch(s)
        assert match is not None, "Не удалось распарсить курс обмена."
        price1 = float(match.group(4))
        currency1 = parse_currency(match.group(5))
        price2 = float(match.group(7))
        currency2 = parse_currency(match.group(8))
        now_currency = ({currency1, currency2} - {currency}).pop()
        self.currency = now_currency
        return (price2 / price1, now_currency) if now_currency == currency1 else (price1 / price2, now_currency)

    def withdraw(self, currency: enums.Currency, wallet: enums.Wallet,
                 amount: int | float, address: str) -> float:
        """
        Отправляет запрос на вывод средств.

        :param currency: валюта вывода.
        :param wallet: тип кошелька.
        :param amount: сумма.
        :param address: адрес кошелька.
        :return: фактически выведенная сумма (с учётом комиссии).
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        wallet_map = {
            enums.Wallet.QIWI: "qiwi",
            enums.Wallet.YOUMONEY: "fps",
            enums.Wallet.BINANCE: "binance",
            enums.Wallet.TRC: "usdt_trc",
            enums.Wallet.CARD_RUB: "card_rub",
            enums.Wallet.CARD_USD: "card_usd",
            enums.Wallet.CARD_EUR: "card_eur",
            enums.Wallet.WEBMONEY: "wmz",
        }
        payload = {
            "csrf_token": self.csrf_token,
            "currency_id": currency.code,
            "ext_currency_id": wallet_map[wallet],
            "wallet": address,
            "amount_int": str(amount),
        }
        response = self.method("post", "withdraw/withdraw",
                               {"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
                               payload, raise_not_200=True)
        json_response = response.json()
        if json_response.get("error"):
            raise exceptions.WithdrawError(response, json_response.get("msg"))
        return float(json_response.get("amount_ext"))

    def get_wallets(self) -> list[types.Wallet]:
        """Возвращает сохранённые кошельки аккаунта."""
        response = self.method("get", "account/wallets", {}, {}, raise_not_200=True)
        bs = BeautifulSoup(response.content.decode(), "lxml")
        form = bs.find("form", class_="details-editor")
        result = []
        for el in form.find_all("div", class_="form-group"):
            data_n = int(el.get("data-n"))
            detail_id = int(el.find("input", {"name": f"details[{data_n}][detail_id]"})["value"])
            if not detail_id:
                continue
            is_masked = bool(int(el.find("input", {"name": f"details[{data_n}][is_masked]"})["value"]))
            data = el.find("input", {"name": f"details[{data_n}][data]"})["value"]
            type_el = el.find("select", {"name": f"details[{data_n}][type_id]"}).find("option", selected=True)
            result.append(types.Wallet(type_el["value"], data, data_n, detail_id, is_masked, type_el.text))
        return result

    def save_wallets(self, wallets: list[types.Wallet]):
        """Сохраняет кошельки аккаунта."""
        payload = {"csrf_token": self.csrf_token, "cat_id": "wallets"}
        max_n = max((w.data_n for w in wallets if w.data_n is not None), default=-1) + 1
        for wallet in wallets:
            i = wallet.data_n if wallet.data_n is not None else max_n
            if wallet.data_n is None:
                max_n += 1
            payload.update({
                f"details[{i}][detail_id]": wallet.detail_id or 0,
                f"details[{i}][is_masked]": int(wallet.is_masked),
            })
            if not wallet.is_masked:
                payload[f"details[{i}][type_id]"] = wallet.type_id
                payload[f"details[{i}][data]"] = wallet.data
        payload.update({
            f"details[{max_n}][detail_id]": 0,
            f"details[{max_n}][is_masked]": 0,
            f"details[{max_n}][type_id]": "",
            f"details[{max_n}][data]": "",
        })
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        r = self.method("post", "account/details", headers, payload, raise_not_200=True)
        if r.json().get("error"):
            raise Exception(r.json().get("msg"))

    # ══════════════════════════════════════════════════════════════════════════
    # Сообщения
    # ══════════════════════════════════════════════════════════════════════════

    def upload_image(self, image: str | IO[bytes],
                     type_: Literal["chat", "offer"] = "chat") -> int:
        """
        Выгружает изображение на серверы FunPay.

        :param image: путь к файлу или байтовый поток.
        :param type_: ``"chat"`` или ``"offer"``.
        :return: ID загруженного изображения.
        """
        assert type_ in ("chat", "offer")
        if isinstance(image, str):
            with open(image, "rb") as f:
                image_bytes = f.read()
        else:
            image_bytes = image.read() if hasattr(image, "read") else image

        multipart = MultipartEncoder(fields={
            "file": ("image.png", image_bytes, "image/png"),
            "csrf_token": self.csrf_token,
        })
        headers = {
            "accept": "*/*",
            "content-type": multipart.content_type,
            "x-requested-with": "XMLHttpRequest",
        }
        endpoint = "chat/upload" if type_ == "chat" else "lots/offerImage"
        response = self.method("post", endpoint, headers, multipart, raise_not_200=True)
        json_response = response.json()
        if json_response.get("error"):
            raise exceptions.ImageUploadError(response, json_response.get("error"))
        return int(json_response["id"])

    def send_message(self, chat_id: int | str, text: str | None,
                     chat_name: Optional[str] = None,
                     interlocutor_id: Optional[int] = None,
                     image_id: Optional[int] = None,
                     add_to_ignore_list: bool = True,
                     update_last_saved_message: bool = False,
                     leave_as_unread: bool = False) -> types.Message:
        """
        Отправляет текстовое сообщение или изображение в чат.

        :param chat_id: ID чата.
        :param text: текст сообщения (None при отправке изображения).
        :param chat_name: никнейм собеседника.
        :param interlocutor_id: ID собеседника.
        :param image_id: ID изображения (если нужно отправить фото).
        :param add_to_ignore_list: добавить ID сообщения в список игнорирования Runner'а.
        :param update_last_saved_message: обновить последнее сохранённое сообщение в Runner'е.
        :param leave_as_unread: оставить чат непрочитанным после отправки.
        :return: объект отправленного сообщения.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()

        request = {
            "action": "chat_message",
            "data": {"node": chat_id, "last_message": -1, "content": ""},
        }
        if image_id is not None:
            request["data"]["image_id"] = image_id
        else:
            request["data"]["content"] = f"{self.__bot_character}{text}" if text else ""

        chats_data = None if leave_as_unread else {chat_id: chat_name}
        response = self.abuse_runner(chats_data=chats_data, request=request)
        json_response = response.json()

        if not (resp := json_response.get("response")):
            raise exceptions.MessageNotDeliveredError(response, None, chat_id)
        if (error_text := resp.get("error")) is not None:
            if error_text in ("Нельзя отправлять сообщения слишком часто.",
                              "You cannot send messages too frequently.",
                              "Не можна надсилати повідомлення занадто часто."):
                self.last_flood_err_time = time.time()
            elif error_text in ("Нельзя слишком часто отправлять сообщения разным пользователям.",
                                "You cannot message multiple users too frequently.",
                                "Не можна надто часто надсилати повідомлення різним користувачам."):
                self.last_multiuser_flood_err_time = time.time()
            raise exceptions.MessageNotDeliveredError(response, error_text, chat_id)

        obj = next((i for i in json_response["objects"]
                    if i["type"] == "chat_node" and
                    chat_id in (i["data"]["node"]["id"],
                                str(i["data"]["node"]["id"]),
                                i["data"]["node"]["name"])), None)

        is_private_chat = True
        if obj is None:
            fake_html = (f'<div class="chat-msg-item" id="message-0000000000">'
                         f'<div class="chat-msg-body"><div class="chat-msg-text">{text}</div></div></div>')
            message_obj = types.Message(0, text, chat_id, chat_name, interlocutor_id,
                                        self.username, self.id, fake_html)
        else:
            tag = obj["tag"]
            mes = obj["data"]["messages"][-1]
            parser = BeautifulSoup(mes["html"].replace("<br>", "\n"), "lxml")
            image_link = image_name = message_text = None
            chat_id = obj["data"]["node"]["id"]
            is_private_chat = not obj["data"]["node"]["silent"]
            try:
                if image_tag := parser.find("a", {"class": "chat-img-link"}):
                    img = image_tag.find("img")
                    image_name = img.get("alt") if img else None
                    image_link = image_tag.get("href")
                else:
                    message_text = (parser.find("div", {"class": "chat-msg-text"}).text
                                    .replace(self.__bot_character, "", 1))
            except Exception as e:
                logger.debug("SEND_MESSAGE RESPONSE: %s", response.content.decode())
                raise e
            message_obj = types.Message(int(mes["id"]), message_text, chat_id, chat_name,
                                        interlocutor_id, self.username, self.id,
                                        mes["html"], image_link, image_name, tag=tag)

        if self.runner and is_private_chat and isinstance(chat_id, int):
            if add_to_ignore_list and message_obj.id:
                self.runner.mark_as_by_bot(chat_id, message_obj.id)
            if update_last_saved_message:
                self.runner.update_last_message(chat_id, message_obj.id, message_obj.text)
        return message_obj

    def send_image(self, chat_id: int, image: int | str | IO[bytes],
                   chat_name: Optional[str] = None,
                   interlocutor_id: Optional[int] = None,
                   add_to_ignore_list: bool = True,
                   update_last_saved_message: bool = False,
                   leave_as_unread: bool = False) -> types.Message:
        """
        Отправляет изображение в личный чат.

        :param chat_id: ID чата.
        :param image: ID изображения (int), путь к файлу (str) или байтовый поток.
        :param chat_name: никнейм собеседника.
        :param interlocutor_id: ID собеседника.
        :param add_to_ignore_list: добавить в список игнорирования Runner'а.
        :param update_last_saved_message: обновить последнее сообщение в Runner'е.
        :param leave_as_unread: оставить чат непрочитанным.
        :return: объект отправленного сообщения.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if not isinstance(image, int):
            image = self.upload_image(image, type_="chat")
        return self.send_message(chat_id, None, chat_name, interlocutor_id,
                                 image, add_to_ignore_list, update_last_saved_message, leave_as_unread)

    # ══════════════════════════════════════════════════════════════════════════
    # Отзывы
    # ══════════════════════════════════════════════════════════════════════════

    def send_review(self, order_id: str, text: str,
                    rating: Literal[1, 2, 3, 4, 5] = 5) -> str:
        """
        Отправляет или редактирует отзыв / ответ на отзыв.

        :param order_id: ID заказа.
        :param text: текст отзыва.
        :param rating: оценка (1–5).
        :return: HTML-код блока отзыва из ответа FunPay.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        text = text.strip()
        payload = {
            "authorId": self.id,
            "text": f"{text}{self.__bot_character}" if text else text,
            "rating": rating or "",
            "csrf_token": self.csrf_token,
            "orderId": order_id,
        }
        response = self.method("post", "orders/review",
                               {"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
                               payload)
        if response.status_code == 400:
            raise exceptions.FeedbackEditingError(response, response.json().get("msg"), order_id)
        elif response.status_code != 200:
            raise exceptions.RequestFailedError(response)
        return response.json().get("content")

    def delete_review(self, order_id: str) -> str:
        """
        Удаляет отзыв / ответ на отзыв.

        :param order_id: ID заказа.
        :return: HTML-код блока отзыва из ответа FunPay.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        payload = {
            "authorId": self.id,
            "csrf_token": self.csrf_token,
            "orderId": order_id,
        }
        response = self.method("post", "orders/reviewDelete",
                               {"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
                               payload)
        if response.status_code == 400:
            raise exceptions.FeedbackEditingError(response, response.json().get("msg"), order_id)
        elif response.status_code != 200:
            raise exceptions.RequestFailedError(response)
        return response.json().get("content")

    def refund(self, order_id: str) -> None:
        """
        Оформляет возврат средств за заказ.

        :param order_id: ID заказа (без «#»).
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        payload = {"id": order_id, "csrf_token": self.csrf_token}
        response = self.method("post", "orders/refund",
                               {"accept": "*/*",
                                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                                "x-requested-with": "XMLHttpRequest"},
                               payload, raise_not_200=True)
        if response.json().get("error"):
            raise exceptions.RefundError(response, response.json().get("msg"), order_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Заказы
    # ══════════════════════════════════════════════════════════════════════════

    def get_orders_by_ids(self, *order_ids: str,
                          include_details: bool = True,
                          include_users: bool = True,
                          include_review: bool = True,
                          locale: Literal["ru", "en", "uk"] | None = None
                          ) -> dict[str, types.Order]:
        """
        Получает полную информацию о нескольких заказах одним батч-запросом
        через новый API ``/api/orders/get``.

        :param order_ids: 1–10 ID заказов.
        :param include_details: включить поля лота.
        :param include_users: включить данные покупателя/продавца.
        :param include_review: включить отзыв.
        :param locale: локаль ответа.
        :return: словарь {order_id: Order}.
        """
        if not 1 <= len(order_ids) <= 10:
            raise ValueError("order_ids должен содержать от 1 до 10 элементов.")
        include = []
        if include_details:
            include.append("details")
        if include_users:
            include.append("users")
        if include_review:
            include.append("review")
        locale = locale or self.__order_parse_locale or self.locale or "ru"
        headers = {"Content-Type": "application/json", "Accept-Language": locale}
        r = self.method("post", "https://funpay.com/api/orders/get",
                        headers=headers,
                        payload=json.dumps({"order_uids": list(order_ids), "include": include}),
                        raise_not_200=True)
        d = r.json()
        if d.get("status") != "SUCCESS" or "data" not in d:
            raise exceptions.RequestFailedError(response=r)
        return {oid: self.__parse_order(data, locale) for oid, data in d["data"].items()}

    def get_order(self, order_id: str,
                  include_details: bool = True,
                  include_users: bool = True,
                  include_review: bool = True,
                  locale: Literal["ru", "en", "uk"] | None = None) -> types.Order:
        """
        Получает полную информацию об одном заказе.

        :param order_id: ID заказа.
        :return: объект заказа.
        """
        return self.get_orders_by_ids(order_id,
                                      include_details=include_details,
                                      include_users=include_users,
                                      include_review=include_review,
                                      locale=locale)[order_id]

    def get_sales(self, start_from: str | None = None,
                  include_paid: bool = True, include_closed: bool = True,
                  include_refunded: bool = True, exclude_ids: list[str] | None = None,
                  id: Optional[str] = None, buyer: Optional[str] = None,
                  state: Optional[Literal["closed", "paid", "refunded"]] = None,
                  game: Optional[int] = None, section: Optional[str] = None,
                  server: Optional[int] = None, side: Optional[int] = None,
                  locale: Literal["ru", "en", "uk"] | None = None,
                  subcategories: dict[str, tuple[types.SubCategoryTypes, int]] | None = None,
                  **more_filters,
                  ) -> tuple[str | None, list[types.OrderShortcut],
                             Literal["ru", "en", "uk"],
                             dict[str, types.SubCategory] | None]:
        """
        Получает список продаж со страницы ``/orders/trade``.

        :param start_from: ID заказа для пагинации (без «#»).
        :param include_paid: включить ожидающие выполнения.
        :param include_closed: включить закрытые.
        :param include_refunded: включить с возвратом.
        :param exclude_ids: исключить заказы с этими ID.
        :param id: фильтр по ID заказа.
        :param buyer: фильтр по никнейму покупателя.
        :param state: фильтр по статусу.
        :param game: фильтр по ID игры.
        :param section: фильтр по секции (``lot-256``, ``chip-4471``).
        :param server: фильтр по ID сервера.
        :param side: фильтр по ID стороны.
        :param locale: принудительная локаль.
        :param subcategories: кэш подкатегорий для маппинга (заполняется автоматически).
        :return: (ID следующего заказа, список заказов, локаль, словарь подкатегорий).
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        exclude_ids = exclude_ids or []
        filters = {k: v for k, v in {
            "id": id, "buyer": buyer, "state": state, "game": game,
            "section": section, "server": server, "side": side,
        }.items() if v}
        filters.update(more_filters)

        link = "https://funpay.com/orders/trade"
        if filters:
            link += "?" + "&".join(f"{k}={v}" for k, v in filters.items())
        if start_from:
            filters["continue"] = start_from

        locale_req = locale or self.__profile_parse_locale
        response = self.method("post" if start_from else "get",
                               link, {}, filters, raise_not_200=True, locale=locale_req)
        if not start_from:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")

        if not start_from and not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)

        next_order_id = parser.find("input", {"type": "hidden", "name": "continue"})
        next_order_id = next_order_id.get("value") if next_order_id else None

        order_divs = parser.find_all("a", {"class": "tc-item"})

        if not start_from:
            subcategories = {}
            app_data = json.loads(parser.find("body").get("data-app-data"))
            locale_req = app_data.get("locale")
            self.csrf_token = app_data.get("csrf-token") or self.csrf_token
            games_options = parser.find("select", attrs={"name": "game"})
            if games_options:
                for game_option in games_options.find_all(lambda x: x.name == "option" and x.get("value")):
                    game_name = game_option.text
                    for key, section_name in json.loads(game_option.get("data-data")):
                        stype_str, sid_str = key.split("-")
                        stype = (types.SubCategoryTypes.COMMON if stype_str == "lot"
                                 else types.SubCategoryTypes.CURRENCY)
                        subcategories[f"{game_name}, {section_name}"] = self.get_subcategory(stype, int(sid_str))
            else:
                subcategories = None

        if not order_divs:
            return None, [], locale_req, subcategories

        sales = []
        for div in order_divs:
            classname = div.get("class", [])
            if "warning" in classname:
                if not include_refunded:
                    continue
                order_status = types.OrderStatuses.REFUNDED
            elif "info" in classname:
                if not include_paid:
                    continue
                order_status = types.OrderStatuses.PAID
            else:
                if not include_closed:
                    continue
                order_status = types.OrderStatuses.CLOSED

            order_id = div.find("div", {"class": "tc-order"}).text[1:]
            if order_id in exclude_ids:
                continue

            description = div.find("div", {"class": "order-desc"}).find("div").text
            tc_price_text = div.find("div", {"class": "tc-price"}).text
            price_str, cur_str = tc_price_text.rsplit(maxsplit=1)
            price = float(price_str.replace(" ", ""))
            currency = parse_currency(cur_str)

            buyer_div = div.find("div", {"class": "media-user-name"}).find("span")
            buyer_username = buyer_div.text
            buyer_id = int(buyer_div.get("data-href")[:-1].split("/users/")[1])
            subcategory_name = div.find("div", {"class": "text-muted"}).text
            subcategory = subcategories.get(subcategory_name) if subcategories else None

            order_date_text = div.find("div", {"class": "tc-date-time"}).text
            order_date = utils.parse_funpay_datetime(order_date_text)
            id1, id2 = sorted([buyer_id, self.id])
            chat_id = f"users-{id1}-{id2}"

            sales.append(types.OrderShortcut(
                order_id, description, price, currency, buyer_username, buyer_id,
                chat_id, order_status, order_date, subcategory_name, subcategory, str(div),
            ))
        return next_order_id, sales, locale_req, subcategories

    # ══════════════════════════════════════════════════════════════════════════
    # Чаты
    # ══════════════════════════════════════════════════════════════════════════

    def get_chat_history(self, chat_id: int | str,
                         last_message_id: int | None = None,
                         interlocutor_username: Optional[str] = None,
                         from_id: int = 0) -> list[types.Message]:
        """
        Получает историю чата (до 100 последних сообщений).

        :param chat_id: ID чата.
        :param last_message_id: ID сообщения, начиная с которого получать историю.
        :param interlocutor_username: никнейм собеседника (опционально).
        :param from_id: не включать сообщения с ID меньше этого.
        :return: список сообщений.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if last_message_id is None:
            return self.get_chats_histories({chat_id: interlocutor_username}).get(chat_id, [])

        response = self.method("get",
                               f"chat/history?node={chat_id}&last_message={last_message_id}",
                               {"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
                               {"node": chat_id, "last_message": last_message_id},
                               raise_not_200=True)
        json_response = response.json()
        if not json_response.get("chat") or not json_response["chat"].get("messages"):
            return []
        node = json_response["chat"]["node"]
        chat_id = node["id"]
        if node["silent"]:
            interlocutor_id, is_private = None, False
        else:
            interlocutors = node["name"].split("-")[1:]
            interlocutors.remove(str(self.id))
            interlocutor_id = int(interlocutors[0])
            is_private = True
            if not interlocutor_username and (cs := self.get_chat_by_id(chat_id)):
                interlocutor_username = cs.name
        return self.__parse_messages(json_response["chat"]["messages"], chat_id,
                                     interlocutor_id, interlocutor_username, from_id, is_private)

    def parse_chats_histories(self,
                              chats_data: dict[int | str, str | None] | list[int | str],
                              objects: list[dict]) -> dict[int | str, list[types.Message]]:
        """
        Разбирает объекты runner-ответа и формирует словарь историй чатов.

        :param chats_data: словарь {ID чата: никнейм} или список ID.
        :param objects: объекты из ответа runner/.
        :return: {ID чата: список сообщений}.
        """
        result = {}
        for i in objects:
            if i.get("type") != "chat_node":
                continue
            if not i.get("data"):
                id_ = i.get("id")
                result_ids = ((int(id_), str(id_)) if (isinstance(id_, str) and id_.isdigit()
                                                        or isinstance(id_, int)) else (id_,))
                for rid in result_ids:
                    if rid in (chats_data if isinstance(chats_data, dict) else {}):
                        result[rid] = []
                continue
            node = i["data"]["node"]
            name, id_, tag = node["name"], node["id"], i["tag"]
            keys = {name, str(id_), id_}
            result_ids = keys & set(chats_data.keys() if isinstance(chats_data, dict) else chats_data)
            for rid in result_ids:
                if node["silent"]:
                    interlocutor_id = interlocutor_name = None
                else:
                    interlocutors = name.split("-")[1:]
                    interlocutors.remove(str(self.id))
                    interlocutor_id = int(interlocutors[0])
                    interlocutor_name = (chats_data.get(rid) if isinstance(chats_data, dict) else None)
                    if not interlocutor_name and (cs := self.get_chat_by_id(id_)):
                        interlocutor_name = cs.name
                messages = self.__parse_messages(i["data"]["messages"], id_,
                                                 interlocutor_id, interlocutor_name,
                                                 is_private=not node["silent"], tag=tag)
                result[rid] = messages
        return result

    def get_chats_histories(self,
                            chats_data: dict[int | str, str | None],
                            include_runner_context: bool = False
                            ) -> dict[int | str, list[types.Message]]:
        """
        Получает историю сразу нескольких чатов одним батч-запросом.

        :param chats_data: {ID чата: никнейм} (никнейм = None, если неизвестен).
        :param include_runner_context: использовать кэш Runner'а.
        :return: {ID чата: список сообщений}.
        """
        response = self.abuse_runner(chats_data=chats_data,
                                     include_runner_context=include_runner_context)
        return self.parse_chats_histories(chats_data, response.json()["objects"])

    def get_chat(self, chat_id: int, with_history: bool = True,
                 locale: Literal["ru", "en", "uk"] | None = None) -> types.Chat:
        """
        Получает информацию о личном чате.

        :param chat_id: ID чата.
        :param with_history: получить историю сообщений.
        :param locale: принудительная локаль.
        :return: объект чата.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if not locale:
            locale = self.__chat_parse_locale
        response = self.method("get", f"chat/?node={chat_id}",
                               {"accept": "*/*"}, {}, raise_not_200=True, locale=locale)
        if locale:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        name = (parser.find("div", {"class": "chat-header"})
                .find("div", {"class": "media-user-name"}).find("a").text)
        if name in ("Чат", "Chat"):
            raise ValueError(f"Чат {chat_id} не найден.")
        self.__update_csrf_token(parser)
        chat_panel = parser.find("div", {"class": "param-item chat-panel"})
        text, link = (None, None) if not chat_panel else (chat_panel.find("a").text, chat_panel.find("a")["href"])
        history = self.get_chats_histories({chat_id: name}).get(chat_id, []) if with_history else []
        return types.Chat(chat_id, name, link, text, html_response, history)

    def request_chats(self) -> list[types.ChatShortcut]:
        """
        Запрашивает список чатов через runner (до 50 чатов).

        :return: список объектов чатов.
        """
        response = self.abuse_runner(last_msg_event_tag=utils.random_tag())
        json_response = response.json()
        msgs_html = ""
        for obj in json_response["objects"]:
            if obj.get("type") == "chat_bookmarks":
                msgs_html = obj["data"]["html"]
        if not msgs_html:
            return []

        parser = BeautifulSoup(msgs_html, "lxml")
        result = []
        for msg in parser.find_all("a", {"class": "contact-item"}):
            chat_id = int(msg["data-id"])
            last_msg_text = msg.find("div", {"class": "contact-item-message"}).text
            unread = "unread" in msg.get("class", [])
            chat_with = msg.find("div", {"class": "media-user-name"}).text
            node_msg_id = int(msg.get("data-node-msg"))
            user_msg_id = int(msg.get("data-user-msg"))
            by_bot = by_vertex = False
            is_image = last_msg_text in ("Изображение", "Зображення", "Image")
            if last_msg_text.startswith(self.bot_character):
                last_msg_text, by_bot = last_msg_text[1:], True
            elif last_msg_text.startswith(self.old_bot_character):
                last_msg_text, by_vertex = last_msg_text[1:], True
            chat_obj = types.ChatShortcut(chat_id, chat_with, last_msg_text,
                                          node_msg_id, user_msg_id, unread, str(msg))
            if not is_image:
                chat_obj.last_by_bot = by_bot
                chat_obj.last_by_vertex = by_vertex
            result.append(chat_obj)
        return result

    def add_chats(self, chats: list[types.ChatShortcut]):
        """Сохраняет чаты в кэш."""
        for chat in chats:
            self.__saved_chats[chat.id] = chat

    def get_chats(self, update: bool = False) -> dict[int, types.ChatShortcut]:
        """
        Возвращает кэшированные чаты.

        :param update: обновить кэш перед возвратом.
        :return: {ID чата: ChatShortcut}.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if update:
            self.add_chats(self.request_chats())
        return self.__saved_chats

    def get_chat_by_name(self, name: str, make_request: bool = False) -> types.ChatShortcut | None:
        """Возвращает чат по имени собеседника."""
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        for chat in self.__saved_chats.values():
            if chat.name == name:
                return chat
        if make_request:
            self.add_chats(self.request_chats())
            return self.get_chat_by_name(name)
        return None

    def get_chat_by_id(self, chat_id: int, make_request: bool = False) -> types.ChatShortcut | None:
        """Возвращает чат по ID."""
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if chat_id in self.__saved_chats or not make_request:
            return self.__saved_chats.get(chat_id)
        self.add_chats(self.request_chats())
        return self.__saved_chats.get(chat_id)

    # ══════════════════════════════════════════════════════════════════════════
    # Профиль пользователя
    # ══════════════════════════════════════════════════════════════════════════

    def get_user(self, user_id: int,
                 locale: Literal["ru", "en", "uk"] | None = None) -> types.UserProfile:
        """
        Парсит профиль пользователя.

        :param user_id: ID пользователя.
        :param locale: принудительная локаль.
        :return: объект профиля.
        """
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        if not locale:
            locale = self.__profile_parse_locale
        response = self.method("get", f"users/{user_id}/",
                               {"accept": "*/*"}, {}, raise_not_200=True, locale=locale)
        if locale:
            self.locale = self.__default_locale
        html_response = response.content.decode()
        parser = BeautifulSoup(html_response, "lxml")
        if not parser.find("div", {"class": "user-link-name"}):
            raise exceptions.UnauthorizedError(response)
        self.__update_csrf_token(parser)

        username = parser.find("span", {"class": "mr4"}).text
        user_status = parser.find("span", {"class": "media-user-status"})
        user_status = user_status.text if user_status else ""
        avatar_link = parser.find("div", {"class": "avatar-photo"}).get("style").split("(")[1].split(")")[0]
        if not avatar_link.startswith("https"):
            avatar_link = f"https://funpay.com{avatar_link}"
        banned = bool(parser.find("span", {"class": "label label-danger"}))
        user_obj = types.UserProfile(user_id, username, avatar_link,
                                     "Онлайн" in user_status or "Online" in user_status,
                                     banned, html_response)

        for subcat_div in parser.find_all("div", {"class": "offer-list-title-container"}):
            link = subcat_div.find("h3").find("a").get("href")
            subcat_id = int(link.split("/")[-2])
            stype = (types.SubCategoryTypes.CURRENCY if "chips" in link
                     else types.SubCategoryTypes.COMMON)
            subcategory_obj = self.get_subcategory(stype, subcat_id)
            if not subcategory_obj:
                continue
            currency = None
            for j in subcat_div.parent.find_all("a", {"class": "tc-item"}):
                offer_id = j["href"].split("id=")[1]
                description = j.find("div", {"class": "tc-desc-text"})
                description = description.text if description else None
                server = j.find("div", class_="tc-server")
                server = server.text if server else None
                side = j.find("div", class_="tc-side")
                side = side.text if side else None
                auto = j.find("i", class_="auto-dlv-icon") is not None
                tc_price = j.find("div", {"class": "tc-price"})
                tc_amount = j.find("div", class_="tc-amount")
                amount = tc_amount.text.replace(" ", "") if tc_amount else None
                amount = int(amount) if amount and amount.isdigit() else None
                if subcategory_obj.type is types.SubCategoryTypes.COMMON:
                    price = float(tc_price["data-s"])
                else:
                    price = float(tc_price.find("div").text.rsplit(maxsplit=1)[0].replace(" ", ""))
                if currency is None:
                    currency = parse_currency(tc_price.find("span", class_="unit").text)
                    if self.currency != currency:
                        self.currency = currency
                lot_obj = types.LotShortcut(offer_id, server, side, description, amount, price,
                                            currency, subcategory_obj, None, auto, None, None, str(j))
                user_obj.add_lot(lot_obj)
        return user_obj

    # ══════════════════════════════════════════════════════════════════════════
    # Покупатель смотрит
    # ══════════════════════════════════════════════════════════════════════════

    def get_buyer_viewing(self, buyer_id: int) -> types.BuyerViewing:
        """Возвращает что просматривает покупатель (по ID покупателя)."""
        json_result = self.abuse_runner(buyer_viewing_ids=[buyer_id]).json()
        for obj in json_result["objects"]:
            if obj["type"] == "c-p-u" and obj["id"] == int(buyer_id):
                return self.__parse_buyer_viewing(obj)
        return types.BuyerViewing(buyer_id, None, None, None, None)

    def get_buyers_viewing(self, *ids: int) -> dict[int, types.BuyerViewing]:
        """Возвращает что просматривают несколько покупателей."""
        json_result = self.abuse_runner(buyer_viewing_ids=list(ids)).json()
        result = {}
        for obj in json_result["objects"]:
            if obj["type"] == "c-p-u" and obj["id"] in ids:
                result[obj["id"]] = self.__parse_buyer_viewing(obj)
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # Категории и подкатегории
    # ══════════════════════════════════════════════════════════════════════════

    def get_category(self, category_id: int) -> types.Category | None:
        return self.__sorted_categories.get(category_id)

    @property
    def categories(self) -> list[types.Category]:
        return self.__categories

    def get_sorted_categories(self) -> dict[int, types.Category]:
        return self.__sorted_categories

    def get_subcategory(self, subcategory_type: types.SubCategoryTypes,
                        subcategory_id: int) -> types.SubCategory | None:
        return self.__sorted_subcategories[subcategory_type].get(subcategory_id)

    @property
    def subcategories(self) -> list[types.SubCategory]:
        return self.__subcategories

    def get_sorted_subcategories(self) -> dict[types.SubCategoryTypes, dict[int, types.SubCategory]]:
        return self.__sorted_subcategories

    # ══════════════════════════════════════════════════════════════════════════
    # Прочее
    # ══════════════════════════════════════════════════════════════════════════

    def logout(self) -> None:
        """Выходит с аккаунта FunPay (инвалидирует golden_key)."""
        if not self.is_initiated:
            raise exceptions.AccountNotInitiatedError()
        self.method("get", self._logout_link, {"accept": "*/*"}, {}, raise_not_200=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Приватные методы
    # ══════════════════════════════════════════════════════════════════════════

    def __update_csrf_token(self, parser: BeautifulSoup):
        try:
            app_data = json.loads(parser.find("body").get("data-app-data"))
            self.csrf_token = app_data.get("csrf-token") or self.csrf_token
        except Exception:
            logger.warning("Произошла ошибка при обновлении csrf-token.")
            logger.debug("TRACEBACK", exc_info=True)

    @staticmethod
    def __parse_buyer_viewing(json_buyer_viewing: dict) -> types.BuyerViewing:
        buyer_id = json_buyer_viewing.get("id")
        if not json_buyer_viewing["data"]:
            return types.BuyerViewing(buyer_id, None, None, None, None)
        tag = json_buyer_viewing["tag"]
        html_data = json_buyer_viewing["data"]["html"]
        if html_data:
            html_data = html_data["desktop"]
            element = BeautifulSoup(html_data, "lxml").find("a")
            link, text = element.get("href"), element.text
        else:
            html_data = link = text = None
        return types.BuyerViewing(buyer_id, link, text, tag, html_data)

    def __parse_order(self, order_data: dict,
                      locale: Literal["ru", "en", "uk"]) -> types.Order:
        id_ = order_data["order_uid"]
        node_id = order_data["section"]["local_id"]
        stype = (types.SubCategoryTypes.COMMON if order_data["section"]["type_id"] == "lot"
                 else types.SubCategoryTypes.CURRENCY)
        subcategory = self.get_subcategory(stype, node_id)
        buyer = order_data["buyer"]
        seller = order_data["seller"]
        currency = parse_currency(order_data["currency"])
        price = float(order_data["amount"])
        status = {
            "unpaid": enums.OrderStatuses.UNPAID,
            "paid": enums.OrderStatuses.PAID,
            "closed": enums.OrderStatuses.CLOSED,
            "refunded": enums.OrderStatuses.REFUNDED,
            "partially_refunded": enums.OrderStatuses.PARTIALLY_REFUNDED,
        }[order_data["status"]]
        chat_id = order_data["chat"]["node_name"]
        review_data = order_data.get("review")
        review = None
        if review_data:
            rtext = review_data["text"]
            rrating = review_data["rating"]
            rreply = review_data["reply"]
            rhidden = review_data["hidden"]
            if rtext or rrating or rreply:
                review = types.Review(
                    rrating, rtext, rreply, False, "", rhidden, id_,
                    buyer.get("name"), buyer["user_id"],
                    bool(rtext and rtext.endswith(self.bot_character)),
                    bool(rreply and rreply.endswith(self.bot_character)),
                )
        type_data = order_data.get("type_data", {})
        amount = type_data.get("amount")
        if amount:
            amount = float(amount)
            amount = int(amount) if int(amount) == amount else amount
        player = type_data.get("player") or None
        secrets = [i["value"] for i in type_data.get("secrets", [])]
        server_data = type_data.get("server")
        server = types.Server(server_data["server_id"], server_data.get("name")) if server_data else None
        side_data = type_data.get("side")
        side = types.Side(side_data["side_id"], side_data.get("name")) if side_data else None
        fields = {
            k: types.LotField(k, v["value"], v["name"], v["field_type_id"])
            for k, v in type_data.get("fields", {}).items()
        }
        return types.Order(id_, status, subcategory, server, side, fields, amount, price,
                           currency, player, buyer["user_id"], buyer.get("name"),
                           seller["user_id"], seller.get("name"), chat_id, review, secrets, locale)

    def __parse_messages(self, json_messages: list, chat_id: int | str,
                         interlocutor_id: Optional[int] = None,
                         interlocutor_username: Optional[str] = None,
                         from_id: int = 0,
                         is_private: bool | None = None,
                         tag: str | None = None) -> list[types.Message]:
        messages = []
        ids = {self.id: self.username, 0: "FunPay"}
        badges = {}
        mb_private = (is_private or interlocutor_id or interlocutor_username
                      or (is_private is None and self.chat_id_private(chat_id)))
        if None not in (interlocutor_id, interlocutor_username):
            ids[interlocutor_id] = interlocutor_username

        for i in json_messages:
            if i["id"] < from_id:
                continue
            author_id = i["author"]
            parser = BeautifulSoup(i["html"].replace("<br>", "\n"), "lxml")

            if None in [ids.get(author_id), badges.get(author_id)] and (
                    author_div := parser.find("div", {"class": "media-user-name"})):
                if badges.get(author_id) is None:
                    badge = author_div.find("span", {"class": "chat-msg-author-label label label-success"})
                    badges[author_id] = badge.text if badge else 0
                if ids.get(author_id) is None:
                    author_name = author_div.find("a").text.strip()
                    ids[author_id] = author_name
                    if mb_private:
                        if author_id == interlocutor_id and not interlocutor_username:
                            interlocutor_username = author_name
                        elif interlocutor_username == author_name and not interlocutor_id:
                            interlocutor_id = author_id

            by_bot = by_vertex = False
            image_link = image_name = None
            if mb_private and (image_tag := parser.find("a", {"class": "chat-img-link"})):
                img = image_tag.find("img")
                image_name = img.get("alt") if img else None
                image_link = image_tag.get("href")
                message_text = None
                if isinstance(image_name, str) and "funpay_cardinal" in image_name.lower():
                    by_bot = True
                elif image_name == "funpay_vertex_image.png":
                    by_vertex = True
            else:
                if author_id == 0:
                    alert = parser.find("div", role="alert") or parser.find("div", {"class": "alert alert-with-icon alert-info"})
                    message_text = alert.text.strip() if alert else ""
                else:
                    message_text = parser.find("div", {"class": "chat-msg-text"}).text
                if (message_text.startswith(self.__bot_character) or
                        (message_text.startswith(self.__old_bot_character) and author_id == self.id)):
                    message_text = message_text[1:]
                    by_bot = True

            msg_obj = types.Message(i["id"], message_text, chat_id, interlocutor_username,
                                    interlocutor_id, None, author_id, i["html"],
                                    image_link, image_name, determine_msg_type=False, tag=tag)
            msg_obj.by_bot = by_bot
            msg_obj.by_vertex = by_vertex
            msg_obj.type = (types.MessageTypes.NON_SYSTEM if author_id != 0
                            else msg_obj.get_message_type())
            messages.append(msg_obj)

        for i in messages:
            i.author = ids.get(i.author_id)
            i.chat_name = interlocutor_username
            i.interlocutor_id = interlocutor_id
            badge_val = badges.get(i.author_id)
            i.badge = badge_val if badge_val and badge_val != 0 else None
            if i.badge:
                i.is_employee = True
                parser = BeautifulSoup(i.html, "lxml")
                default_label = parser.find("div", {"class": "media-user-name"})
                default_label = (default_label.find("span", {
                    "class": "chat-msg-author-label label label-default"
                }) if default_label else None)
                if default_label:
                    if default_label.text in ("автовідповідь", "автоответ", "auto-reply"):
                        i.is_autoreply = True
                if i.badge in ("поддержка", "підтримка", "support"):
                    i.is_support = True
                elif i.badge in ("модерация", "модерація", "moderation"):
                    i.is_moderation = True
                elif i.badge in ("арбитраж", "арбітраж", "arbitration"):
                    i.is_arbitration = True
            if i.type != types.MessageTypes.NON_SYSTEM:
                parser = BeautifulSoup(i.html, "lxml")
                users = parser.find_all("a", href=lambda h: h and "/users/" in h)
                if users:
                    i.initiator_username = users[0].text
                    i.initiator_id = int(users[0]["href"].split("/")[-2])
                    if i.type in (types.MessageTypes.ORDER_PURCHASED,
                                  types.MessageTypes.ORDER_CONFIRMED,
                                  types.MessageTypes.NEW_FEEDBACK,
                                  types.MessageTypes.FEEDBACK_CHANGED,
                                  types.MessageTypes.FEEDBACK_DELETED):
                        i.i_am_seller = i.initiator_id != self.id
                        i.i_am_buyer = i.initiator_id == self.id
                    elif i.type in (types.MessageTypes.NEW_FEEDBACK_ANSWER,
                                    types.MessageTypes.FEEDBACK_ANSWER_CHANGED,
                                    types.MessageTypes.FEEDBACK_ANSWER_DELETED,
                                    types.MessageTypes.REFUND):
                        i.i_am_seller = i.initiator_id == self.id
                        i.i_am_buyer = i.initiator_id != self.id
                    elif len(users) > 1:
                        last_user_id = int(users[-1]["href"].split("/")[-2])
                        if i.type == types.MessageTypes.ORDER_CONFIRMED_BY_ADMIN:
                            i.i_am_seller = last_user_id == self.id
                            i.i_am_buyer = last_user_id != self.id
                        elif i.type == types.MessageTypes.REFUND_BY_ADMIN:
                            i.i_am_seller = last_user_id != self.id
                            i.i_am_buyer = last_user_id == self.id
        return messages

    def __setup_categories(self, html_str: str):
        parser = BeautifulSoup(html_str, "lxml")
        games_table = parser.find_all("div", {"class": "promo-game-list"})
        if not games_table:
            return
        games_table = games_table[1] if len(games_table) > 1 else games_table[0]
        games_divs = games_table.find_all("div", {"class": "promo-game-item"})
        if not games_divs:
            return

        game_position = 0
        subcategory_position = 0
        for i in games_divs:
            gid = int(i.find("div", {"class": "game-title"}).get("data-id"))
            gname = i.find("a").text
            regional_games = {gid: types.Category(gid, gname, position=game_position)}
            game_position += 1
            if regional_divs := i.find("div", {"role": "group"}):
                for btn in regional_divs.find_all("button"):
                    rgid = int(btn["data-id"])
                    regional_games[rgid] = types.Category(rgid, f"{gname} ({btn.text})",
                                                          position=game_position)
                    game_position += 1
            for j in i.find_all("ul", {"class": "list-inline"}):
                j_game_id = int(j["data-id"])
                for k in j.find_all("li"):
                    a = k.find("a")
                    name, link = a.text, a["href"]
                    stype = (types.SubCategoryTypes.CURRENCY if "chips" in link
                             else types.SubCategoryTypes.COMMON)
                    sid = int(link.split("/")[-2])
                    sobj = types.SubCategory(sid, name, stype,
                                             regional_games[j_game_id], subcategory_position)
                    subcategory_position += 1
                    regional_games[j_game_id].add_subcategory(sobj)
                    self.__subcategories.append(sobj)
                    self.__sorted_subcategories[stype][sid] = sobj
            for gid, gobj in regional_games.items():
                self.__categories.append(gobj)
                self.__sorted_categories[gid] = gobj