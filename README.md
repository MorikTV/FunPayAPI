# FunPayAPI

Неофициальная Python-библиотека для взаимодействия с платформой [FunPay](https://funpay.com).

Предоставляет удобный интерфейс для автоматизации работы с аккаунтом: управление лотами, чаты, заказы, отзывы и многое другое. Библиотека активно поддерживается и включает расширенный набор функций по сравнению с аналогами — улучшенную обработку ошибок, поддержку локалей, батч-запросы и надёжную систему повторных попыток при сбоях сети.

---

## Возможности

- 🔐 **Авторизация** через `golden_key`
- 🛒 **Лоты** — получение, создание, редактирование, удаление, поднятие
- 💬 **Чаты** — история сообщений, отправка текста и изображений, батч-запросы
- 📦 **Заказы** — список продаж, детальная информация, возвраты, батч-получение
- ⭐ **Отзывы** — добавление, редактирование, удаление
- 💰 **Баланс и вывод средств** — просмотр баланса, отправка на кошелёк
- 👁 **«Покупатель смотрит»** — отслеживание просматриваемых лотов
- 🔄 **Runner** — система событий в реальном времени (новые сообщения, заказы, смена статусов)
- 🌍 **Мультиязычность** — поддержка `ru`, `en`, `uk`
- ♻️ **Авто-retry** — устойчивость к 429, 500, 502, 503, 504 ошибкам
- 🚀 **Батчинг запросов** — объединение нескольких запросов в один для экономии лимитов

---

## Установка

```bash
pip install requests beautifulsoup4 lxml requests-toolbelt
```

Затем скопируй папку `FunPayAPI/` в свой проект.

---

## Быстрый старт

```python
from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

account = Account(golden_key="ВАШ_GOLDEN_KEY")
account.get()

print(f"Привет, {account.username}!")

runner = Runner(account)

for event in runner.listen():
    if isinstance(event, NewMessageEvent):
        print(f"Новое сообщение от {event.message.author}: {event.message.text}")

    elif isinstance(event, NewOrderEvent):
        print(f"Новый заказ от {event.order.buyer_username} на {event.order.price} {event.order.currency}")
```

---

## Структура пакета

```
FunPayAPI/
├── account.py          # Основной класс Account
├── types.py            # Все типы данных
├── common/
│   ├── enums.py        # Перечисления
│   ├── exceptions.py   # Исключения
│   └── utils.py        # Вспомогательные функции
└── updater/
    ├── events.py       # Классы событий
    └── runner.py       # Runner — получение событий
```

---

## Получение golden_key

1. Открой [funpay.com](https://funpay.com) и войди в аккаунт
2. Открой DevTools → **F12**
3. Перейди во вкладку **Application** (Chrome) или **Storage** (Firefox)
4. Выбери **Cookies → https://funpay.com**
5. Скопируй значение куки `golden_key`

> ⚠️ Никому не передавай свой `golden_key` — это токен доступа к твоему аккаунту.

---

## Лицензия

[MIT](LICENSE)