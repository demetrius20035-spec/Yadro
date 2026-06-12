# -*- coding: utf-8 -*-
"""
Генератор XSD-схем XML-формата выгрузки данных из метаданных 1С.

Из JSON-описания метаданных (полная выгрузка конфигурации) создаёт по одному
.xsd-файлу на каждый объект метаданных (справочник / документ / регистр).
Эти XSD — КОНТРАКТ ФОРМАТА: по ним команда пишет в 1С обработку-выгрузчик,
которая формирует XML, и загрузчик, который этот XML читает.

=== Формат XML (что описывает каждая XSD) ===

Корень файла объекта:

  <Выгрузка Конфигурация="Мега" ВерсияКонфигурации="..."
            Объект="Справочник.Контрагенты" ВерсияФормата="1.0"
            ДатаВыгрузки="2026-06-12T11:18:45">
    <Запись>
      <!-- примитив: значение текстом внутри элемента -->
      <Наименование>ООО Ромашка</Наименование>
      <Код>00-000123</Код>
      <ПометкаУдаления>false</ПометкаУдаления>

      <!-- ссылочное / составное поле: вложенный элемент Ссылка.
           Тип = полное имя объекта метаданных, Guid = GUID записи.
           Если ссылка ещё не разрешена при выгрузке — Тип/Guid могут быть
           пустыми; это штатная точка ОТЛОЖЕННОГО РАЗРЕШЕНИЯ ссылок. -->
      <Родитель>
        <Ссылка>
          <Тип>Справочник.Контрагенты</Тип>
          <Guid>a1b2...-...-...</Guid>
        </Ссылка>
      </Родитель>

      <!-- составной реквизит с примитивами среди вариантов:
           к Ссылке добавляется элемент Значение под примитивное значение,
           когда фактический тип значения — примитив, а не ссылка. -->
      <Значение>
        <Ссылка><Тип/><Guid/></Ссылка>
        <Значение>текст или число строкой</Значение>
      </Значение>

      <!-- табличная часть: коллекция строк -->
      <ТабличнаяЧасть_Товары>
        <Строка НомерСтроки="1">
          <Номенклатура><Ссылка><Тип>Справочник.Номенклатура</Тип><Guid>..</Guid></Ссылка></Номенклатура>
          <Количество>10</Количество>
        </Строка>
      </ТабличнаяЧасть_Товары>
    </Запись>
    <Запись> ... </Запись>
  </Выгрузка>

Для регистра накопления корневой элемент тот же <Выгрузка>, но вместо
<Запись> идут <Движение> с атрибутом НомерСтроки и полями:
Период, Регистратор (ссылка), Активность, [ВидДвижения для остатков],
далее Измерения / Ресурсы / Реквизиты.

=== Соответствие типов 1С -> XSD ===
  Строка                 -> xs:string (с xs:maxLength, если Длина>0)
  Число                  -> xs:decimal (xs:totalDigits / xs:fractionDigits)
  Дата (Дата)            -> xs:date
  Дата (Дата и время)    -> xs:dateTime
  Булево                 -> xs:boolean
  ХранилищеЗначения      -> xs:base64Binary
  УникальныйИдентификатор-> xs:string (pattern GUID)
  Ссылочный / составной  -> вложенный комплексный тип "Ссылка"

Зависимостей нет (стандартная библиотека xml.etree).
Запуск:
    python3 generate_xml_schema.py metadata.json -o ./schemas
        --> создаёт каталог schemas/ с *.xsd по одному на объект + общий
            tns:Ссылка и заголовок в каждом файле.
"""

import json
import os
import sys
import argparse
import xml.etree.ElementTree as ET

XS = "http://www.w3.org/2001/XMLSchema"
GUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

ET.register_namespace("xs", XS)


def xs(tag):
    return f"{{{XS}}}{tag}"


def el(parent, tag, **attrs):
    e = ET.SubElement(parent, xs(tag))
    for k, v in attrs.items():
        e.set(k, str(v))
    return e


# ------------------------------------------------------------------ #
# Описание типа поля -> XSD-узел внутри <xs:element name=...>
# ------------------------------------------------------------------ #
def add_simple_restriction(element, t1):
    """Внутрь element кладёт xs:simpleType с restriction по примитиву."""
    name = t1.get("ИмяТипа", "")
    st = el(element, "simpleType")

    if name == "Строка":
        r = el(st, "restriction", base="xs:string")
        length = t1.get("Длина", 0)
        if length and length > 0:
            el(r, "maxLength", value=length)
    elif name == "Число":
        r = el(st, "restriction", base="xs:decimal")
        prec = t1.get("Разрядность", 15)
        scale = t1.get("РазрядностьДробнойЧасти", 0)
        el(r, "totalDigits", value=prec)
        el(r, "fractionDigits", value=scale)
    elif name == "Дата":
        base = "xs:date" if t1.get("ЧастиДаты") == "Дата" else "xs:dateTime"
        el(st, "restriction", base=base)
    elif name == "Булево":
        el(st, "restriction", base="xs:boolean")
    elif name in ("ХранилищеЗначения", "Хранилище значения"):
        el(st, "restriction", base="xs:base64Binary")
    elif name == "УникальныйИдентификатор":
        r = el(st, "restriction", base="xs:string")
        el(r, "pattern", value=GUID_PATTERN)
    else:
        el(st, "restriction", base="xs:string")


def field_element(parent, attr, optional=True):
    """Создаёт <xs:element> для одного реквизита 1С.

    Ссылочные/составные -> type="Ссылка" (или расширенный с Значение).
    Примитивы -> вложенный simpleType.
    """
    fname = attr["Имя"]
    t = attr.get("Тип", {})
    types = t.get("Типы", [])
    is_composite = t.get("Составной", False)
    minocc = "0" if optional else "1"

    # Несоставной с единственным примитивом
    if not is_composite and len(types) == 1 and not types[0].get("Ссылочный") \
            and types[0].get("ИмяТипа") != "УникальныйИдентификатор":
        e = el(parent, "element", name=fname, minOccurs=minocc)
        if attr.get("Синоним"):
            annotate(e, attr["Синоним"])
        add_simple_restriction(e, types[0])
        return

    # Стандартный реквизит "Ссылка" = идентификатор самой записи.
    # Кодируется напрямую: <Ссылка><Тип/><Guid/></Ссылка> (без обёртки).
    if fname == "Ссылка":
        e = el(parent, "element", name="Ссылка", type="Ссылка", minOccurs=minocc)
        annotate(e, "Идентификатор самой записи (тип + GUID)")
        return

    # Несоставной с единственным ссылочным типом ->
    # единообразно: <Поле><Ссылка><Тип/><Guid/></Ссылка></Поле>
    if not is_composite and len(types) == 1 and types[0].get("Ссылочный"):
        e = el(parent, "element", name=fname, minOccurs=minocc)
        if attr.get("Синоним"):
            annotate(e, attr["Синоним"])
        ct = el(e, "complexType")
        seq = el(ct, "sequence")
        el(seq, "element", name="Ссылка", type="Ссылка")
        return

    # УникальныйИдентификатор как отдельный простой
    if not is_composite and len(types) == 1 and types[0].get("ИмяТипа") == "УникальныйИдентификатор":
        e = el(parent, "element", name=fname, minOccurs=minocc)
        if attr.get("Синоним"):
            annotate(e, attr["Синоним"])
        add_simple_restriction(e, types[0])
        return

    # Составной (в т.ч. пустой "Типы":[] = неразрешённая ссылка).
    has_primitive = any(not x.get("Ссылочный") for x in types)
    e = el(parent, "element", name=fname, minOccurs=minocc)
    if attr.get("Синоним"):
        annotate(e, attr["Синоним"])
    ct = el(e, "complexType")
    seq = el(ct, "sequence")
    # вложенный <Ссылка> (тип+guid)
    el(seq, "element", name="Ссылка", type="Ссылка")
    if has_primitive:
        # элемент Значение под примитивное значение строкой (тип см. в Ссылка/Тип)
        zn = el(seq, "element", name="Значение", type="xs:string", minOccurs="0")
        annotate(zn, "Примитивное значение составного типа (текстом)")


def annotate(element, text):
    a = el(element, "annotation")
    d = el(a, "documentation")
    d.text = text


# ------------------------------------------------------------------ #
# Общий тип "Ссылка" — добавляется в каждую схему
# ------------------------------------------------------------------ #
def add_ssylka_type(schema):
    ct = el(schema, "complexType", name="Ссылка")
    annotate(ct, "Ссылка на объект: Тип = полное имя метаданных, Guid = GUID "
                 "записи. Пустые Тип/Guid допускаются для отложенного разрешения.")
    seq = el(ct, "sequence")
    et_ = el(seq, "element", name="Тип", minOccurs="0")
    add_simple_restriction(et_, {"ИмяТипа": "Строка", "Длина": 255})
    eg = el(seq, "element", name="Guid", minOccurs="0")
    eg_st = el(eg, "simpleType")
    r = el(eg_st, "restriction", base="xs:string")
    el(r, "pattern", value=GUID_PATTERN)


# ------------------------------------------------------------------ #
# Построение схемы для объекта
# ------------------------------------------------------------------ #
def new_schema():
    schema = ET.Element(xs("schema"), {
        "elementFormDefault": "qualified",
    })
    return schema


def add_root_export_element(schema, obj, record_tag, record_builder):
    """Создаёт корневой <xs:element name="Выгрузка"> с атрибутами и
    коллекцией record_tag, тело которой строит record_builder(seq)."""
    e = el(schema, "element", name="Выгрузка")
    annotate(e, obj.get("Синоним", obj["ПолноеИмя"]))
    ct = el(e, "complexType")
    seq = el(ct, "sequence")
    rec = el(seq, "element", name=record_tag, minOccurs="0", maxOccurs="unbounded")
    rct = el(rec, "complexType")
    rseq = el(rct, "sequence")
    record_builder(rseq, rct)
    # атрибуты заголовка выгрузки
    for an in ("Конфигурация", "ВерсияКонфигурации", "Объект",
               "ВерсияФормата", "ДатаВыгрузки"):
        el(ct, "attribute", name=an, type="xs:string")


def build_catalog_document(schema, obj):
    full = obj["ПолноеИмя"]

    def record_builder(rseq, rct):
        for attr in obj.get("СтандартныеРеквизиты", []):
            field_element(rseq, attr)
        for attr in obj.get("Реквизиты", []):
            field_element(rseq, attr)
        # табличные части — вложенные коллекции
        for tp in obj.get("ТабличныеЧасти", []):
            tp_el = el(rseq, "element", name="ТабличнаяЧасть_" + tp["Имя"],
                       minOccurs="0")
            if tp.get("Синоним"):
                annotate(tp_el, tp["Синоним"])
            tp_ct = el(tp_el, "complexType")
            tp_seq = el(tp_ct, "sequence")
            row = el(tp_seq, "element", name="Строка", minOccurs="0",
                     maxOccurs="unbounded")
            row_ct = el(row, "complexType")
            row_seq = el(row_ct, "sequence")
            for rattr in tp.get("Реквизиты", []):
                field_element(row_seq, rattr)
            el(row_ct, "attribute", name="НомерСтроки", type="xs:integer")

    add_root_export_element(schema, obj, "Запись", record_builder)


def build_register(schema, obj):
    vid = obj.get("ВидРегистра", "Обороты")

    def record_builder(rseq, rct):
        # Период
        per = el(rseq, "element", name="Период", type="xs:dateTime", minOccurs="0")
        annotate(per, "Период движения")
        # Регистратор — ссылка (единый формат с вложенным Ссылка)
        reg = el(rseq, "element", name="Регистратор", minOccurs="0")
        annotate(reg, "Документ-регистратор движения")
        reg_ct = el(reg, "complexType")
        reg_seq = el(reg_ct, "sequence")
        el(reg_seq, "element", name="Ссылка", type="Ссылка")
        # Активность
        el(rseq, "element", name="Активность", type="xs:boolean", minOccurs="0")
        if vid == "Остатки":
            vd = el(rseq, "element", name="ВидДвижения", minOccurs="0")
            vd_st = el(vd, "simpleType")
            r = el(vd_st, "restriction", base="xs:string")
            el(r, "enumeration", value="Приход")
            el(r, "enumeration", value="Расход")
        for attr in obj.get("Измерения", []):
            field_element(rseq, attr)
        for attr in obj.get("Ресурсы", []):
            field_element(rseq, attr)
        for attr in obj.get("Реквизиты", []):
            field_element(rseq, attr)
        el(rct, "attribute", name="НомерСтроки", type="xs:integer")

    add_root_export_element(schema, obj, "Движение", record_builder)


def indent(elem, level=0):
    """Красивые отступы (Python 3.8 не имеет ET.indent)."""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for sub in elem:
            indent(sub, level + 1)
            if not sub.tail or not sub.tail.strip():
                sub.tail = i + "  "
        if not sub.tail or not sub.tail.strip():
            sub.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


HANDLERS = {
    "Справочник": build_catalog_document,
    "Документ": build_catalog_document,
    "РегистрНакопления": build_register,
}


def safe_filename(full_name):
    return full_name.replace(".", "_") + ".xsd"


def generate(data, outdir):
    os.makedirs(outdir, exist_ok=True)
    written = []
    skipped = []
    for obj in data.get("Объекты", []):
        typ = obj.get("Тип")
        handler = HANDLERS.get(typ)
        if not handler:
            skipped.append(f"{typ}: {obj.get('ПолноеИмя','')}")
            continue
        schema = new_schema()
        add_ssylka_type(schema)
        handler(schema, obj)
        indent(schema)
        tree = ET.ElementTree(schema)
        fname = os.path.join(outdir, safe_filename(obj["ПолноеИмя"]))
        with open(fname, "wb") as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding="utf-8", xml_declaration=False)
        written.append(fname)
    return written, skipped


README_TEXT = """# XML-формат выгрузки данных 1С

Этот каталог содержит XSD-схемы — **контракт формата** выгрузки данных из
конфигурации «{cfg}» ({ver}). По одной схеме на каждый объект метаданных.
Обработка-выгрузчик в 1С формирует XML по этим схемам; загрузчик читает их.

## Файлы
Каждый объект -> отдельный `.xsd` (имя = ПолноеИмя с `.`→`_`), и при выгрузке
данных -> отдельный XML-файл на объект с корнем `<Выгрузка>`.

## Корень
```xml
<Выгрузка Конфигурация="..." ВерсияКонфигурации="..." Объект="Справочник.X"
          ВерсияФормата="1.0" ДатаВыгрузки="2026-06-12T11:18:45">
  <Запись> ... </Запись>   <!-- справочник/документ -->
  ...
</Выгрузка>
```
У регистров вместо `<Запись>` идут `<Движение НомерСтроки="N">`.

## Ссылки (ключевой момент)
Любое ссылочное/составное поле кодируется вложенным элементом `<Ссылка>`:
```xml
<Контрагент>
  <Ссылка><Тип>Справочник.Контрагенты</Тип><Guid>GUID</Guid></Ссылка>
</Контрагент>
```
- `Тип` — полное имя объекта метаданных, куда указывает ссылка.
- `Guid` — GUID записи-цели.
- **Отложенное разрешение:** если на момент выгрузки цель ещё не выгружена
  (или ссылка пустая), допускается пустой `<Ссылка/>` либо пустые `Тип`/`Guid`.
  Загрузчик обязан уметь принять запись с такой ссылкой и до-связать её позже.

Поле `Ссылка` самой записи — это её идентификатор, кодируется НАПРЯМУЮ:
```xml
<Ссылка><Тип>Справочник.X</Тип><Guid>GUID</Guid></Ссылка>
```

Составной тип, среди вариантов которого есть примитивы, имеет доп. элемент
`Значение` под примитивное значение строкой:
```xml
<Значение>
  <Ссылка><Тип/><Guid/></Ссылка>     <!-- если значение — ссылка -->
  <Значение>текст или число</Значение> <!-- если значение — примитив -->
</Значение>
```

## Табличные части
```xml
<ТабличнаяЧасть_Товары>
  <Строка НомерСтроки="1"> ...поля строки... </Строка>
</ТабличнаяЧасть_Товары>
```

## Регистры накопления
`<Движение>` содержит: `Период`, `Регистратор` (ссылка), `Активность`,
для регистров остатков — `ВидДвижения` (Приход/Расход), далее Измерения,
Ресурсы, Реквизиты. Естественный ключ строки — (`Регистратор`, `НомерСтроки`).

## Типы значений (примитивы)
| 1С | XML |
|----|-----|
| Строка | текст (maxLength = Длина, если >0) |
| Число | десятичное (totalDigits/fractionDigits) |
| Дата (Дата) | `ГГГГ-ММ-ДД` (xs:date) |
| Дата (Дата и время) | `ГГГГ-ММ-ДДTчч:мм:сс` (xs:dateTime) |
| Булево | `true` / `false` |
| ХранилищеЗначения | base64 |
| УникальныйИдентификатор | строка-GUID |
"""


def write_readme(data, outdir):
    txt = README_TEXT.format(cfg=data.get("ИмяКонфигурации", ""),
                             ver=data.get("ВерсияКонфигурации", ""))
    path = os.path.join(outdir, "README.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Генератор XSD-схем XML-формата выгрузки из метаданных 1С")
    ap.add_argument("input", help="путь к metadata.json")
    ap.add_argument("-o", "--output", default="./schemas",
                    help="каталог для .xsd (по умолчанию ./schemas)")
    ap.add_argument("--no-readme", action="store_true",
                    help="не создавать README.md с описанием формата")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    written, skipped = generate(data, args.output)
    print(f"Создано XSD: {len(written)} (каталог {args.output})")
    for w in written:
        print("  ", os.path.basename(w))
    if not args.no_readme:
        rp = write_readme(data, args.output)
        print("Создан:", os.path.basename(rp))
    if skipped:
        print("Пропущено (необрабатываемый тип):")
        for s in skipped:
            print("  ", s)


if __name__ == "__main__":
    main()
