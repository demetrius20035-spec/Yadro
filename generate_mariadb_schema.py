# -*- coding: utf-8 -*-
"""
Генератор схемы таблиц MariaDB из метаданных 1С (JSON полной выгрузки).

Принципы маппинга (согласованы):
  * Имя таблицы: "Справочник.БанковскиеСчета" -> "Справочник_БанковскиеСчета".
    При длине > 64 символов имя усекается и к нему добавляется _<hash8>.
  * Ссылочный тип (Ссылочный=true) -> CHAR(36) (GUID записи, на которую ссылаемся).
  * Составной реквизит -> CHAR(36) под значение-GUID + VARCHAR(255) "<Имя>_Тип"
    под полное имя объекта метаданных, чтобы знать, куда указывает GUID.
    Если в составной тип входят и примитивы — добавляется ещё "<Имя>_Знач" TEXT
    под примитивное значение строкой (тип отличаем по "_Тип").
  * Примитивы:
      Строка фикс/перем, Длина>0  -> VARCHAR(Длина)
      Строка, Длина=0 (неогр.)    -> TEXT
      Число(Разрядность,Дробная)  -> DECIMAL(Разрядность, РазрядностьДробнойЧасти)
      Дата, ЧастиДаты="Дата"      -> DATE
      Дата, иначе                 -> DATETIME
      Булево                      -> TINYINT(1)
      ХранилищеЗначения           -> LONGBLOB
  * Каждая таблица объекта получает PRIMARY KEY по полю "Ссылка" CHAR(36)
    (GUID самой записи). Если поля Ссылка нет (бывает у регистров) — см. ниже.
  * Иерархия: поля Родитель / ЭтоГруппа уже идут в СтандартныхРеквизитах,
    отдельная обработка не требуется — они разложатся как обычные поля.
  * Владельцы: стандартный реквизит "Владелец". Если он составной (несколько
    владельцев) — раскладывается как составной (CHAR(36)+_Тип).
  * Табличные части -> отдельная таблица "<ИмяОбъекта>_<ИмяТЧ>" с полями:
      Ссылка CHAR(36)  (GUID шапки-владельца строки),
      НомерСтроки INT,
      + реквизиты ТЧ.
    PRIMARY KEY (Ссылка, НомерСтроки).
  * Регистры накопления: таблица с полями Период DATETIME, Регистратор CHAR(36),
    Регистратор_Тип VARCHAR (ссылка на документ-регистратор; составная по сути),
    НомерСтроки INT, Активность TINYINT, ВидДвижения TINYINT (для оборотных не
    обязателен, но добавляем универсально), затем Измерения, Ресурсы, Реквизиты.
    Ключ записи: для регистра уникальность строки задаётся
    (Регистратор, НомерСтроки) — это естественный ключ движений в 1С,
    поэтому PRIMARY KEY (Регистратор, НомерСтроки).
  * FOREIGN KEY НЕ создаются (отложенное разрешение ссылок). Вместо них —
    обычные INDEX на все GUID-колонки (ссылочные и составные значения).
  * При существующей таблице: DROP TABLE IF EXISTS, всё обёрнуто в
    SET FOREIGN_KEY_CHECKS=0/1.

Вывод: один .sql файл. Зависимостей нет (стандартная библиотека).
Запуск:
    python3 generate_mariadb_schema.py metadata.json -o schema.sql
"""

import json
import sys
import hashlib
import argparse

MAX_IDENT = 64           # лимит длины идентификатора MariaDB
GUID_LEN = 36
DEFAULT_VARCHAR_FOR_COMPOSITE_TYPE = 255  # под полное имя объекта метаданных

PRIMITIVE_NAMES = {"Строка", "Число", "Дата", "Булево",
                   "ХранилищеЗначения", "Хранилище значения",
                   "УникальныйИдентификатор"}


def ident(name: str) -> str:
    """Делает безопасный идентификатор MariaDB из имени 1С."""
    safe = name.replace(".", "_")
    if len(safe) <= MAX_IDENT:
        return safe
    h = hashlib.md5(safe.encode("utf-8")).hexdigest()[:8]
    return safe[:MAX_IDENT - 9] + "_" + h


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def column_type_for_simple(t1: dict) -> str:
    """Тип колонки для НЕсоставного типа (один элемент Типы)."""
    name = t1.get("ИмяТипа", "")
    if t1.get("Ссылочный"):
        return f"CHAR({GUID_LEN})"
    if name == "Строка":
        length = t1.get("Длина", 0)
        if not length or length <= 0:
            return "TEXT"
        return f"VARCHAR({length})"
    if name == "Число":
        prec = t1.get("Разрядность", 15)
        scale = t1.get("РазрядностьДробнойЧасти", 0)
        prec = max(prec, scale + 1)  # MariaDB требует precision >= scale (+знак)
        return f"DECIMAL({prec}, {scale})"
    if name == "Дата":
        parts = t1.get("ЧастиДаты", "Дата и время")
        return "DATE" if parts == "Дата" else "DATETIME"
    if name == "Булево":
        return "TINYINT(1)"
    if name in ("ХранилищеЗначения", "Хранилище значения"):
        return "LONGBLOB"
    if name == "УникальныйИдентификатор":
        return f"CHAR({GUID_LEN})"
    # неизвестный непомеченный тип — на всякий случай TEXT
    return "TEXT"


def emit_columns_for_attr(attr: dict):
    """Возвращает список (имя_колонки, sql_тип, is_guid_index) для реквизита.

    Для составного типа возвращает 2-3 колонки.
    """
    col_name = attr["Имя"]
    t = attr.get("Тип", {})
    types = t.get("Типы", [])
    is_composite = t.get("Составной", False)

    # Несоставной с одним типом
    if not is_composite and len(types) == 1:
        sqltype = column_type_for_simple(types[0])
        is_guid = types[0].get("Ссылочный", False) or \
            types[0].get("ИмяТипа") == "УникальныйИдентификатор"
        return [(col_name, sqltype, is_guid)]

    # Несоставной, но Типы пуст (бывает у "Составной":true,"Типы":[] —
    # неразрешённая ссылка) или странный случай — трактуем как составной.
    # Составной тип: значение-GUID + тип. Плюс примитивное значение, если есть
    # непомеченные ссылкой примитивы среди типов.
    has_ref = any(x.get("Ссылочный") for x in types) or len(types) == 0
    has_primitive = any(not x.get("Ссылочный") for x in types)

    cols = []
    # основная колонка под GUID ссылки (составной почти всегда может быть ссылкой)
    cols.append((col_name, f"CHAR({GUID_LEN})", True))
    # колонка с именем типа (полное имя объекта метаданных или имя примитива)
    cols.append((col_name + "_Тип",
                 f"VARCHAR({DEFAULT_VARCHAR_FOR_COMPOSITE_TYPE})", False))
    # если среди вариантов есть примитивы — отдельная колонка под значение строкой
    if has_primitive:
        cols.append((col_name + "_Знач", "TEXT", False))
    return cols


def build_table(table_name: str, columns: list, pk_cols: list,
                guid_index_cols: list, comment: str = "") -> str:
    """Собирает CREATE TABLE из списка колонок (имя, тип)."""
    lines = []
    seen = set()
    for cname, ctype in columns:
        if cname in seen:
            # дубликат имени колонки (например, два реквизита с одинаковым
            # именем после нормализации) — добавим суффикс
            i = 2
            new = f"{cname}_{i}"
            while new in seen:
                i += 1
                new = f"{cname}_{i}"
            cname = new
        seen.add(cname)
        lines.append(f"  {quote_ident(cname)} {ctype}")

    # первичный ключ
    if pk_cols:
        pk = ", ".join(quote_ident(c) for c in pk_cols if c in seen)
        if pk:
            lines.append(f"  PRIMARY KEY ({pk})")

    # индексы на GUID-колонки.
    # Не дублируем индекс, если колонка уже первая в составном PK
    # (или единственная в PK) — такой индекс избыточен.
    pk_first = pk_cols[0] if pk_cols else None
    for gcol in guid_index_cols:
        if gcol in seen and gcol != pk_first:
            idx_name = ident("idx_" + table_name + "_" + gcol)
            lines.append(f"  KEY {quote_ident(idx_name)} ({quote_ident(gcol)})")

    body = ",\n".join(lines)
    cmt = f" COMMENT={sql_str(comment)}" if comment else ""
    return (f"DROP TABLE IF EXISTS {quote_ident(table_name)};\n"
            f"CREATE TABLE {quote_ident(table_name)} (\n{body}\n)"
            f" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4{cmt};\n")


def sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def process_attr_list(attr_list, columns, guid_cols):
    for attr in attr_list:
        for cname, ctype, is_guid in emit_columns_for_attr(attr):
            columns.append((cname, ctype))
            if is_guid:
                guid_cols.append(cname)


def gen_catalog_or_document(obj: dict) -> list:
    """Возвращает список CREATE TABLE (шапка + табличные части)."""
    full = obj["ПолноеИмя"]
    base_table = ident(full)
    statements = []

    columns = []
    guid_cols = []
    process_attr_list(obj.get("СтандартныеРеквизиты", []), columns, guid_cols)
    process_attr_list(obj.get("Реквизиты", []), columns, guid_cols)

    # PK по "Ссылка", если есть
    col_names = {c[0] for c in columns}
    pk = ["Ссылка"] if "Ссылка" in col_names else []

    statements.append(build_table(base_table, columns, pk, guid_cols,
                                  comment=obj.get("Синоним", "")))

    # Табличные части
    for tp in obj.get("ТабличныеЧасти", []):
        tp_table = ident(full + "." + tp["Имя"])
        tp_columns = [("Ссылка", f"CHAR({GUID_LEN})"),
                      ("НомерСтроки", "INT")]
        tp_guid = ["Ссылка"]
        process_attr_list(tp.get("Реквизиты", []), tp_columns, tp_guid)
        statements.append(build_table(
            tp_table, tp_columns, ["Ссылка", "НомерСтроки"], tp_guid,
            comment=(obj.get("Синоним", "") + " / " + tp.get("Синоним", tp["Имя"]))))

    return statements


def gen_register(obj: dict) -> list:
    """Регистр накопления -> одна таблица движений."""
    full = obj["ПолноеИмя"]
    table = ident(full)
    vid = obj.get("ВидРегистра", "Обороты")

    columns = [
        ("Период", "DATETIME"),
        ("Регистратор", f"CHAR({GUID_LEN})"),
        ("Регистратор_Тип", f"VARCHAR({DEFAULT_VARCHAR_FOR_COMPOSITE_TYPE})"),
        ("НомерСтроки", "INT"),
        ("Активность", "TINYINT(1)"),
    ]
    guid_cols = ["Регистратор"]
    # для регистров остатков вид движения (приход/расход) полезен
    if vid == "Остатки":
        columns.append(("ВидДвижения", "TINYINT(1)"))

    process_attr_list(obj.get("Измерения", []), columns, guid_cols)
    process_attr_list(obj.get("Ресурсы", []), columns, guid_cols)
    process_attr_list(obj.get("Реквизиты", []), columns, guid_cols)

    # Ключ записи движения: (Регистратор, НомерСтроки)
    pk = ["Регистратор", "НомерСтроки"]
    return [build_table(table, columns, pk, guid_cols,
                        comment=f"{obj.get('Синоним','')} ({vid})")]


def generate(data: dict) -> str:
    out = []
    cfg = data.get("ИмяКонфигурации", "")
    ver = data.get("ВерсияКонфигурации", "")
    out.append(f"-- Схема MariaDB, сгенерирована из метаданных 1С")
    out.append(f"-- Конфигурация: {cfg} {ver}")
    out.append(f"-- Ссылки = CHAR(36) GUID, FK не создаются, только индексы.")
    out.append("SET NAMES utf8mb4;")
    out.append("SET FOREIGN_KEY_CHECKS=0;")
    out.append("")

    handlers = {
        "Справочник": gen_catalog_or_document,
        "Документ": gen_catalog_or_document,
        "РегистрНакопления": gen_register,
    }

    count = 0
    for obj in data.get("Объекты", []):
        typ = obj.get("Тип")
        h = handlers.get(typ)
        if not h:
            out.append(f"-- ПРОПУЩЕН необрабатываемый тип: {typ} {obj.get('ПолноеИмя','')}")
            continue
        out.append(f"-- ==== {obj.get('ПолноеИмя','')} ====")
        for stmt in h(obj):
            out.append(stmt)
        count += 1

    out.append("SET FOREIGN_KEY_CHECKS=1;")
    out.append(f"-- Готово. Объектов обработано: {count}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Генератор DDL MariaDB из метаданных 1С")
    ap.add_argument("input", help="путь к metadata.json")
    ap.add_argument("-o", "--output", default="schema.sql", help="выходной .sql")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    sql = generate(data)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(sql)
    print(f"Записано: {args.output} ({len(sql)} символов)")


if __name__ == "__main__":
    main()
