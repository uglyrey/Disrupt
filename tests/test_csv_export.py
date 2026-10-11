import csv
import io

from app.admin_export import make_csv


def test_excel_dialect_preserves_cyrillic_cells_quotes_newlines_and_formula_guard():
    content = make_csv(("Поле", "Значение"), [
        ("Текст", 'Строка; с "кавычками"\nи переносом'),
        ("Цена", "1.93456999"), ("Пусто", None), ("Ноль", 0),
        ("=SUM(1;2)", "@опасная формула"),
    ])
    encoded = content.encode("utf-8")
    assert encoded.startswith(b"\xef\xbb\xbf")
    assert content.startswith("\ufeffПоле;Значение\r\n")
    rows = list(csv.reader(io.StringIO(encoded.decode("utf-8-sig")), delimiter=";"))
    assert rows == [["Поле", "Значение"], ["Текст", 'Строка; с "кавычками"\nи переносом'],
                    ["Цена", "1.93456999"], ["Пусто", ""], ["Ноль", "0"],
                    ["'=SUM(1;2)", "'@опасная формула"]]
    assert all(len(row) == 2 for row in rows)
