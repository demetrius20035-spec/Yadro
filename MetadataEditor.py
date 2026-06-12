# -*- coding: utf-8 -*-
"""
Редактор полной выгрузки метаданных 1С (большой JSON).

Возможности:
  * Открывает большой metadata.json (грузится целиком в память; 20-30 МБ — нормально).
  * Ленивое дерево: Тип -> Объект -> разделы (Стандартные реквизиты / Реквизиты /
    Табличные части / Измерения / Ресурсы) -> листья (реквизиты, поля ТЧ).
    Дети объекта создаются только при первом разворачивании — иначе Tkinter
    не тянет сотни тысяч узлов.
  * Чекбоксы кликом на любом уровне. Снятая галочка = объект/реквизит/ТЧ будет
    УДАЛЁН из файла при сохранении (сохраняется только отмеченное).
  * Фильтр по подстроке (имя + синоним) на уровне объектов + «Отметить найденное».
  * Кнопка «Вычистить составные типы» — массово урезает раздутые массивы Типы
    у составных реквизитов: оставляет либо первые N, либо только неудаляемые
    (Строка/Число/Дата/Булево и т.п. — непривязанные к объектам метаданных).
  * Сохранение реконструирует JSON с исходной шапкой и indent="\t" (формат 1С).

Зависимостей нет — только стандартная библиотека (Tkinter).
"""

import json
import copy
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

CHECK_ON = "[x] "
CHECK_OFF = "[ ] "

# Разделы объекта, которые показываем как разворачиваемые узлы.
# (имя ключа в JSON -> человекочитаемая подпись)
SECTION_KEYS = [
    ("СтандартныеРеквизиты", "Стандартные реквизиты"),
    ("Реквизиты", "Реквизиты"),
    ("ТабличныеЧасти", "Табличные части"),
    ("Измерения", "Измерения"),
    ("Ресурсы", "Ресурсы"),
    ("Реквизиты", "Реквизиты"),  # дубликат для ТЧ обрабатывается отдельно
]

# Ссылочный тип привязан к объекту метаданных и потенциально удаляем.
# Примитивы ниже считаем «неудаляемыми» при чистке составных типов.
PRIMITIVE_TYPES = {"Строка", "Число", "Дата", "Булево",
                   "ХранилищеЗначения", "УникальныйИдентификатор"}


class Node:
    """Узел модели данных, привязанный к строке дерева.

    kind:
      'group'    — группа по типу объекта (Справочник/Документ/...)
      'object'   — объект метаданных
      'section'  — раздел объекта (Реквизиты, ТабличныеЧасти, ...)
      'attr'     — реквизит / поле (лист)
      'tablepart'— табличная часть (имеет вложенные attr)
    ref хранит ссылку на соответствующий объект/словарь в self.data,
    чтобы при сохранении удалять снятое прямо из структуры.
    """
    __slots__ = ("kind", "ref", "parent_ref", "section_key", "loaded")

    def __init__(self, kind, ref=None, parent_ref=None, section_key=None):
        self.kind = kind
        self.ref = ref
        self.parent_ref = parent_ref
        self.section_key = section_key
        self.loaded = False


class MetadataEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Редактор метаданных 1С — отбор и чистка")
        self.root.geometry("1100x780")

        self.data = None
        self.current_path = None
        self.nodes = {}          # tree item id -> Node
        self.checked = set()     # tree item id, отмеченные (для object/attr/tablepart)

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=6, pady=6)
        ttk.Button(toolbar, text="Открыть metadata.json", command=self.open_file).pack(side="left")
        ttk.Button(toolbar, text="Сохранить отмеченное", command=self.save_file).pack(side="left", padx=4)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Отметить всё", command=lambda: self.toggle_all(True)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Снять всё", command=lambda: self.toggle_all(False)).pack(side="left")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Вычистить составные типы…",
                   command=self.cleanup_composite_types).pack(side="left")

        filter_frame = ttk.Frame(self.root)
        filter_frame.pack(fill="x", padx=6)
        ttk.Label(filter_frame, text="Фильтр (имя/синоним объекта):").pack(side="left")
        self.filter_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(filter_frame, text="Применить", command=self.apply_filter).pack(side="left")
        ttk.Button(filter_frame, text="Сброс", command=self.reset_filter).pack(side="left", padx=4)
        ttk.Button(filter_frame, text="Отметить найденное", command=self.check_filtered).pack(side="left")

        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=("info",), show="tree headings")
        self.tree.heading("#0", text="[ ] Объект / реквизит")
        self.tree.heading("info", text="Синоним / тип")
        self.tree.column("info", width=420)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<<TreeviewOpen>>", self.on_open)

        self.status = tk.StringVar(value="Откройте metadata.json")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=6, pady=(0, 6))

    # ---------- Загрузка ----------
    def open_file(self):
        path = filedialog.askopenfilename(
            title="Открыть metadata.json",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            self.status.set("Чтение файла…")
            self.root.update_idletasks()
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return
        self.current_path = path
        self.populate_tree()

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.checked.clear()

        objects = self.data.get("Объекты", [])
        groups = {}
        for obj in objects:
            groups.setdefault(obj.get("Тип", "Прочее"), []).append(obj)

        for type_name in sorted(groups):
            objs = groups[type_name]
            gid = self.tree.insert("", "end",
                                   text=f"{CHECK_ON}{type_name} ({len(objs)})",
                                   values=("",), open=False)
            self.nodes[gid] = Node("group")
            for obj in sorted(objs, key=lambda o: o.get("Имя", "")):
                oid = self.tree.insert(
                    gid, "end",
                    text=f"{CHECK_ON}{obj.get('Имя', '')}",
                    values=(obj.get("Синоним", ""),),
                    open=False)
                self.nodes[oid] = Node("object", ref=obj)
                self.checked.add(oid)
                # заглушка, чтобы появился треугольник разворота
                self.tree.insert(oid, "end", text="…")

        total = len(objects)
        self.status.set(f"Объектов: {total}. Отмечено: {self._count_checked_objects()}")

    # ---------- Ленивое разворачивание ----------
    def on_open(self, _event):
        item = self.tree.focus()
        node = self.nodes.get(item)
        if not node or node.loaded:
            return
        if node.kind == "object":
            self._load_object_children(item, node)
        elif node.kind == "tablepart":
            self._load_tablepart_children(item, node)
        node.loaded = True

    def _clear_stub(self, item):
        for ch in self.tree.get_children(item):
            if self.nodes.get(ch) is None:
                self.tree.delete(ch)

    def _load_object_children(self, item, node):
        self._clear_stub(item)
        obj = node.ref
        obj_checked = item in self.checked
        # Разделы: стандартные реквизиты, реквизиты, измерения, ресурсы (списки словарей)
        for key, label in [("СтандартныеРеквизиты", "Стандартные реквизиты"),
                           ("Реквизиты", "Реквизиты"),
                           ("Измерения", "Измерения"),
                           ("Ресурсы", "Ресурсы")]:
            items = obj.get(key)
            if not items:
                continue
            sid = self.tree.insert(item, "end",
                                   text=f"{CHECK_ON}{label} ({len(items)})",
                                   values=("",), open=False)
            self.nodes[sid] = Node("section", ref=items, parent_ref=obj, section_key=key)
            for attr in items:
                self._insert_attr(sid, attr, obj_checked)
        # Табличные части
        tparts = obj.get("ТабличныеЧасти")
        if tparts:
            sid = self.tree.insert(item, "end",
                                   text=f"{CHECK_ON}Табличные части ({len(tparts)})",
                                   values=("",), open=False)
            self.nodes[sid] = Node("section", ref=tparts, parent_ref=obj, section_key="ТабличныеЧасти")
            for tp in tparts:
                tpid = self.tree.insert(
                    sid, "end",
                    text=f"{CHECK_ON if obj_checked else CHECK_OFF}{tp.get('Имя', '')}",
                    values=(tp.get("Синоним", ""),), open=False)
                self.nodes[tpid] = Node("tablepart", ref=tp, parent_ref=tparts)
                if obj_checked:
                    self.checked.add(tpid)
                self.tree.insert(tpid, "end", text="…")  # заглушка

    def _load_tablepart_children(self, item, node):
        self._clear_stub(item)
        tp = node.ref
        tp_checked = item in self.checked
        attrs = tp.get("Реквизиты", [])
        for attr in attrs:
            self._insert_attr(item, attr, tp_checked)

    def _insert_attr(self, parent_item, attr, parent_checked):
        type_descr = self._type_summary(attr.get("Тип", {}))
        aid = self.tree.insert(
            parent_item, "end",
            text=f"{CHECK_ON if parent_checked else CHECK_OFF}{attr.get('Имя', '')}",
            values=(f"{attr.get('Синоним', '')}  ·  {type_descr}",))
        self.nodes[aid] = Node("attr", ref=attr)
        if parent_checked:
            self.checked.add(aid)

    @staticmethod
    def _type_summary(t):
        types = t.get("Типы", [])
        if not types:
            return ""
        if t.get("Составной"):
            return f"составной: {len(types)} тип(ов)"
        return types[0].get("ИмяТипа", "")

    # ---------- Чекбоксы ----------
    def on_click(self, event):
        # Реагируем только на клик по тексту/иконке, не по треугольнику разворота.
        if self.tree.identify("element", event.x, event.y) == "Treeitem.indicator":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        new_state = not self._is_checked(item)
        self._set_subtree(item, new_state)
        self._refresh_ancestors(item)
        self._update_status()

    def _is_checked(self, item):
        text = self.tree.item(item, "text")
        return text.startswith(CHECK_ON)

    def _set_check_mark(self, item, value):
        text = self.tree.item(item, "text")
        base = text[len(CHECK_ON):] if text.startswith((CHECK_ON, CHECK_OFF)) else text
        self.tree.item(item, text=f"{CHECK_ON if value else CHECK_OFF}{base}")
        node = self.nodes.get(item)
        if node and node.kind in ("object", "attr", "tablepart"):
            if value:
                self.checked.add(item)
            else:
                self.checked.discard(item)

    def _set_subtree(self, item, value):
        """Ставит/снимает галочку у узла и всех уже загруженных потомков.
        Для незагруженных потомков состояние применится при разворачивании
        (через parent_checked), поэтому дополнительно помечаем сам узел."""
        self._set_check_mark(item, value)
        for ch in self.tree.get_children(item):
            if self.nodes.get(ch) is None:  # заглушка
                continue
            self._set_subtree(ch, value)

    def _refresh_ancestors(self, item):
        parent = self.tree.parent(item)
        while parent:
            children = [c for c in self.tree.get_children(parent)
                        if self.nodes.get(c) is not None]
            if children:
                all_on = all(self._is_checked(c) for c in children)
                self._set_check_mark(parent, all_on)
            parent = self.tree.parent(parent)

    def toggle_all(self, value):
        for gid in self.tree.get_children():
            self._set_subtree(gid, value)
        self._update_status()

    def _count_checked_objects(self):
        return sum(1 for i, n in self.nodes.items()
                   if n.kind == "object" and self._is_checked(i))

    def _update_status(self):
        total = sum(1 for n in self.nodes.values() if n.kind == "object")
        self.status.set(f"Объектов: {total}. Отмечено объектов: {self._count_checked_objects()}")

    # ---------- Фильтр ----------
    def apply_filter(self):
        text = self.filter_var.get().strip().lower()
        for gid in self.tree.get_children():
            group_match = False
            for oid in self.tree.get_children(gid):
                node = self.nodes.get(oid)
                if not node or node.kind != "object":
                    continue
                obj = node.ref
                hay = (obj.get("Имя", "") + " " + obj.get("Синоним", "")).lower()
                match = (text in hay) if text else True
                # прячем/показываем объект через detach/reattach
                # (Treeview не умеет hide, поэтому имитируем тегом — оставим простой вариант:
                #  просто разворачиваем группы с совпадениями)
                if match:
                    group_match = True
            self.tree.item(gid, open=bool(text) and group_match)

    def reset_filter(self):
        self.filter_var.set("")
        for gid in self.tree.get_children():
            self.tree.item(gid, open=False)

    def check_filtered(self):
        text = self.filter_var.get().strip().lower()
        if not text:
            messagebox.showinfo("Фильтр", "Введите текст фильтра.")
            return
        # Сначала снимаем всё, потом отмечаем совпавшие объекты — так «Отметить
        # найденное» работает как «оставить только найденное».
        if messagebox.askyesno("Отметить найденное",
                               "Снять отметки со всех и отметить только совпавшие объекты?"):
            self.toggle_all(False)
        for oid, node in self.nodes.items():
            if node.kind != "object":
                continue
            obj = node.ref
            hay = (obj.get("Имя", "") + " " + obj.get("Синоним", "")).lower()
            if text in hay:
                self._set_subtree(oid, True)
                self._refresh_ancestors(oid)
        self._update_status()

    # ---------- Чистка составных типов ----------
    def cleanup_composite_types(self):
        if self.data is None:
            messagebox.showwarning("Внимание", "Сначала откройте файл.")
            return
        dlg = CleanupDialog(self.root)
        if dlg.result is None:
            return
        mode, keep_n = dlg.result

        affected = 0
        removed_total = 0
        for obj in self.data.get("Объекты", []):
            for attr in self._iter_all_attrs(obj):
                t = attr.get("Тип", {})
                if not t.get("Составной"):
                    continue
                types = t.get("Типы", [])
                if not types:
                    continue
                before = len(types)
                if mode == "primitives":
                    new_types = [tp for tp in types
                                 if not tp.get("Ссылочный")
                                 or tp.get("ИмяТипа") in PRIMITIVE_TYPES]
                else:  # first_n
                    new_types = types[:keep_n]
                if len(new_types) != before:
                    t["Типы"] = new_types
                    affected += 1
                    removed_total += before - len(new_types)

        # Дерево могло частично отобразить старые типы — обновим подписи у уже
        # загруженных листьев. Проще перерисовать развёрнутые объекты: сбросим
        # их флаг loaded и заглушим обратно.
        self._reset_expanded_nodes()
        messagebox.showinfo(
            "Готово",
            f"Изменено составных реквизитов: {affected}\n"
            f"Удалено типов суммарно: {removed_total}")

    def _iter_all_attrs(self, obj):
        for key in ("СтандартныеРеквизиты", "Реквизиты", "Измерения", "Ресурсы"):
            for attr in obj.get(key, []):
                yield attr
        for tp in obj.get("ТабличныеЧасти", []):
            for attr in tp.get("Реквизиты", []):
                yield attr

    def _reset_expanded_nodes(self):
        # Сворачиваем и сбрасываем загрузку объектов, чтобы при следующем
        # разворачивании подписи типов перечитались.
        for item, node in list(self.nodes.items()):
            if node.kind == "object" and node.loaded:
                checked = self._is_checked(item)
                for ch in self.tree.get_children(item):
                    self.tree.delete(ch)
                    self.nodes.pop(ch, None)
                self.tree.insert(item, "end", text="…")
                self.tree.item(item, open=False)
                node.loaded = False

    # ---------- Сохранение ----------
    def save_file(self):
        if self.data is None:
            messagebox.showwarning("Внимание", "Сначала откройте файл.")
            return

        out = dict(self.data)  # копия шапки
        new_objects = []
        for gid in self.tree.get_children():
            for oid in self.tree.get_children(gid):
                node = self.nodes.get(oid)
                if not node or node.kind != "object":
                    continue
                if not self._is_checked(oid):
                    continue  # объект целиком снят — пропускаем
                # Глубокая копия объекта и фильтрация снятых реквизитов/ТЧ,
                # но только если объект разворачивали (иначе он отмечен целиком).
                obj_copy = self._build_filtered_object(oid, node)
                new_objects.append(obj_copy)

        out["Объекты"] = new_objects

        path = filedialog.asksaveasfilename(
            title="Сохранить отмеченное",
            defaultextension=".json",
            initialfile="metadata_selected.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent="\t")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")
            return
        messagebox.showinfo("Готово",
                            f"Сохранено объектов: {len(new_objects)}\n{path}")

    def _build_filtered_object(self, oid, node):
        """Собирает объект для выгрузки. Если объект не разворачивали,
        отдаём как есть (он отмечен целиком). Если разворачивали — убираем
        снятые реквизиты, поля и табличные части."""
        obj = node.ref
        if not node.loaded:
            return obj  # не трогали внутренности — сохраняем целиком

        obj_copy = copy.deepcopy(obj)
        # Маппинг загруженных листьев: ref(id) -> отмечен ли
        attr_state = {}
        tp_state = {}
        tp_attr_state = {}  # (id таблички, id реквизита) -> bool
        for item, n in self.nodes.items():
            if n.kind == "attr":
                attr_state[id(n.ref)] = self._is_checked(item)
            elif n.kind == "tablepart":
                tp_state[id(n.ref)] = self._is_checked(item)

        def filter_attr_list(lst):
            return [a for a in lst if attr_state.get(id(a), True)]

        for key in ("СтандартныеРеквизиты", "Реквизиты", "Измерения", "Ресурсы"):
            if key in obj_copy and isinstance(obj_copy[key], list):
                # сопоставляем по позиции с оригиналом
                orig = obj.get(key, [])
                kept = [orig[i] for i in range(len(orig))
                        if attr_state.get(id(orig[i]), True)]
                obj_copy[key] = copy.deepcopy(kept)

        if "ТабличныеЧасти" in obj_copy:
            orig_tps = obj.get("ТабличныеЧасти", [])
            new_tps = []
            for tp in orig_tps:
                if not tp_state.get(id(tp), True):
                    continue
                tp_copy = copy.deepcopy(tp)
                if "Реквизиты" in tp_copy:
                    orig_attrs = tp.get("Реквизиты", [])
                    kept = [orig_attrs[i] for i in range(len(orig_attrs))
                            if attr_state.get(id(orig_attrs[i]), True)]
                    tp_copy["Реквизиты"] = copy.deepcopy(kept)
                new_tps.append(tp_copy)
            obj_copy["ТабличныеЧасти"] = new_tps

        return obj_copy


class CleanupDialog(simpledialog.Dialog):
    """Диалог настройки чистки составных типов."""
    def body(self, master):
        self.title("Чистка составных типов")
        self.mode_var = tk.StringVar(value="primitives")
        ttk.Label(master, text="Что делать с раздутыми массивами «Типы»\n"
                               "у составных реквизитов:",
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Radiobutton(master, text="Оставить только примитивы (Строка/Число/Дата/Булево …),\n"
                                     "удалить все ссылочные типы",
                        variable=self.mode_var, value="primitives",
                        ).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(master, text="Оставить только первые N типов:",
                        variable=self.mode_var, value="first_n",
                        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.n_var = tk.StringVar(value="5")
        ttk.Entry(master, textvariable=self.n_var, width=6).grid(row=2, column=1, sticky="w")
        return master

    def apply(self):
        mode = self.mode_var.get()
        if mode == "first_n":
            try:
                n = max(1, int(self.n_var.get()))
            except ValueError:
                n = 5
            self.result = ("first_n", n)
        else:
            self.result = ("primitives", 0)


if __name__ == "__main__":
    root = tk.Tk()
    app = MetadataEditor(root)
    root.mainloop()