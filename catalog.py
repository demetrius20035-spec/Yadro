import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

class CatalogEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Редактор catalog.json — отбор объектов для выгрузки")
        self.root.geometry("900x700")

        self.data = None          # весь загруженный JSON
        self.checked = set()      # id строк дерева, которые отмечены
        self.node_to_obj = {}     # id строки дерева -> объект из JSON

        self._build_ui()

    def _build_ui(self):
        # Верхняя панель кнопок
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=6, pady=6)

        ttk.Button(toolbar, text="Открыть catalog.json", command=self.open_file).pack(side="left")
        ttk.Button(toolbar, text="Сохранить отмеченное", command=self.save_file).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Отметить всё", command=lambda: self.toggle_all(True)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Снять всё", command=lambda: self.toggle_all(False)).pack(side="left")

        # Поле фильтра
        filter_frame = ttk.Frame(self.root)
        filter_frame.pack(fill="x", padx=6)
        ttk.Label(filter_frame, text="Фильтр:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.apply_filter())
        ttk.Entry(filter_frame, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(filter_frame, text="Отметить найденное", command=self.check_filtered).pack(side="left")

        # Дерево
        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=6)

        self.tree = ttk.Treeview(tree_frame, columns=("synonym",), show="tree headings")
        self.tree.heading("#0", text="[ ] Объект")
        self.tree.heading("synonym", text="Синоним")
        self.tree.column("synonym", width=300)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self.on_click)

        # Статусная строка
        self.status = tk.StringVar(value="Откройте catalog.json")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=6, pady=(0, 6))

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Открыть catalog.json",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось прочитать файл:\n{e}")
            return

        self.current_path = path
        self.populate_tree()

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.node_to_obj.clear()

        objects = self.data.get("Объекты", [])
        # Группируем по типу
        groups = {}
        for obj in objects:
            groups.setdefault(obj.get("Тип", "Прочее"), []).append(obj)

        for type_name in sorted(groups):
            group_id = self.tree.insert("", "end", text=f"[ ] {type_name} ({len(groups[type_name])})", open=False)
            for obj in sorted(groups[type_name], key=lambda o: o.get("Имя", "")):
                name = obj.get("Имя", "")
                synonym = obj.get("Синоним", "")
                node_id = self.tree.insert(group_id, "end", text=f"[ ] {name}", values=(synonym,))
                self.node_to_obj[node_id] = obj

        total = len(objects)
        self.status.set(f"Загружено объектов: {total}. Отмечено: 0")

    def on_click(self, event):
        # Клик по строке переключает галочку
        node_id = self.tree.identify_row(event.y)
        if not node_id:
            return
        # Если это группа — переключаем всех детей
        children = self.tree.get_children(node_id)
        if children:
            # группа
            target = not all(c in self.checked for c in children)
            for c in children:
                self._set_check(c, target)
            self._refresh_group_label(node_id)
        else:
            self._set_check(node_id, node_id not in self.checked)
            parent = self.tree.parent(node_id)
            if parent:
                self._refresh_group_label(parent)
        self._update_status()

    def _set_check(self, node_id, value):
        if node_id not in self.node_to_obj:
            return
        text = self.tree.item(node_id, "text")
        clean = text[4:] if text.startswith(("[ ] ", "[x] ")) else text
        if value:
            self.checked.add(node_id)
            self.tree.item(node_id, text=f"[x] {clean}")
        else:
            self.checked.discard(node_id)
            self.tree.item(node_id, text=f"[ ] {clean}")

    def _refresh_group_label(self, group_id):
        children = self.tree.get_children(group_id)
        checked_count = sum(1 for c in children if c in self.checked)
        text = self.tree.item(group_id, "text")
        # вытащим имя типа без префикса
        base = text[4:] if text.startswith(("[ ] ", "[x] ")) else text
        mark = "x" if checked_count == len(children) and children else " "
        self.tree.item(group_id, text=f"[{mark}] {base}")

    def toggle_all(self, value):
        for node_id in self.node_to_obj:
            self._set_check(node_id, value)
        for group_id in self.tree.get_children():
            self._refresh_group_label(group_id)
        self._update_status()

    def apply_filter(self):
        # Раскрываем/подсвечиваем совпадения — простой вариант: разворачиваем группы с совпадениями
        text = self.filter_var.get().strip().lower()
        if not text:
            return
        for group_id in self.tree.get_children():
            has_match = False
            for node_id in self.tree.get_children(group_id):
                obj = self.node_to_obj.get(node_id, {})
                hay = (obj.get("Имя", "") + " " + obj.get("Синоним", "")).lower()
                if text in hay:
                    has_match = True
            self.tree.item(group_id, open=has_match)

    def check_filtered(self):
        text = self.filter_var.get().strip().lower()
        if not text:
            return
        for node_id, obj in self.node_to_obj.items():
            hay = (obj.get("Имя", "") + " " + obj.get("Синоним", "")).lower()
            if text in hay:
                self._set_check(node_id, True)
        for group_id in self.tree.get_children():
            self._refresh_group_label(group_id)
        self._update_status()

    def _update_status(self):
        total = len(self.node_to_obj)
        self.status.set(f"Загружено объектов: {total}. Отмечено: {len(self.checked)}")

    def save_file(self):
        if self.data is None:
            messagebox.showwarning("Внимание", "Сначала откройте catalog.json")
            return
        if not self.checked:
            if not messagebox.askyesno("Подтверждение", "Ничего не отмечено. Сохранить пустой список объектов?"):
                return

        selected_objects = [self.node_to_obj[n] for n in self.node_to_obj if n in self.checked]

        out = dict(self.data)  # копируем шапку (ВерсияФормата, ИмяКонфигурации и т.д.)
        out["Объекты"] = selected_objects

        path = filedialog.asksaveasfilename(
            title="Сохранить отмеченное",
            defaultextension=".json",
            initialfile="catalog_selected.json",
            filetypes=[("JSON", "*.json")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent="\t")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}")
            return
        messagebox.showinfo("Готово", f"Сохранено объектов: {len(selected_objects)}\n{path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = CatalogEditor(root)
    root.mainloop()