from rpa.hosvital.parsing import (bottom_row_by_left, find_item_index, has_grid_header, is_icon_size, match_layout,
                                  month_matches, parse_grid_rows, parse_rango_header, parse_row, state_from_titles)


def test_filas_de_la_grilla_desde_el_ocr():
    lines = [
        [("Tipo", 24, 6, 27, 10), ("de", 56, 5, 15, 9), ("Atención", 76, 5, 56, 9)],
        [("Consecutivo", 152, 6, 78, 8), ("De", 235, 6, 16, 8), ("Ingresos", 256, 6, 57, 10)],
        [("AMBULATORIO", 22, 39, 89, 9), ("7", 308, 39, 6, 9), ("07/10/2026", 321, 38, 73, 12), ("09:20:00", 400, 38, 60, 12)],
        [("AMBULATORIO", 22, 22, 89, 9), ("6", 308, 22, 6, 9), ("07/10/2026", 321, 21, 73, 12)],
    ]
    rows = parse_grid_rows(lines)
    assert [(r.numero, r.fecha, r.hora) for r in rows] == [("6", "07/10/2026", None), ("7", "07/10/2026", "09:20:00")]
    assert (rows[0].x, rows[0].y) == (311, 26.5)           # centro de la celda del consecutivo, ordenadas por y
    assert has_grid_header(lines)


def test_filas_aunque_el_ocr_separe_numero_y_fecha_en_lineas_distintas():
    lines = [[("AMBULATORIO", 22, 24, 89, 9)], [("6", 300, 24, 6, 9)], [("07/10/2026", 321, 23, 73, 12)],
             [("17", 301, 41, 12, 9)], [("07/10/2026", 321, 40, 73, 12)]]
    assert [(r.numero, r.fecha) for r in parse_grid_rows(lines)] == [("6", "07/10/2026"), ("17", "07/10/2026")]


def test_titulo_del_rango_con_el_consecutivo():
    w = lambda t, i: (t, 20 + 40 * i, 38, 38, 12)  # noqa: E731
    titulo = "Consecutivo de Ingreso 6 del 07/10/2026 07:00:00 Servicio AMBULATORIO".split()
    assert parse_rango_header([[w(t, i) for i, t in enumerate(titulo)]]) == ("6", "07/10/2026", "07:00:00")
    # el OCR puede partir el título en varias líneas o no traer la hora
    partido = [[w("Consecutivo", 0), w("de", 1), w("Ingreso", 2)], [w("17", 3), w("del", 4), w("07/10/2026", 5)]]
    assert parse_rango_header(partido) == ("17", "07/10/2026", None)
    assert parse_rango_header([[w("Rango", 0), w("de", 1), w("Impresion", 2)]]) is None


def test_grilla_vacia_o_ocr_fallido():
    cabecera = [[("Consecutivo", 152, 6, 78, 8), ("Ingresos", 256, 6, 57, 10)]]
    assert parse_grid_rows(cabecera) == [] and has_grid_header(cabecera)        # grilla legible pero sin filas
    assert parse_grid_rows([]) == [] and not has_grid_header([])                # el OCR no leyó nada
    # un número y una fecha en alturas distintas no forman una fila
    assert parse_grid_rows([[("6", 308, 22, 6, 9), ("07/10/2026", 321, 80, 73, 12)]]) == []


def test_parse_row_con_datos_y_vacia():
    r = parse_row(["", "", "", "71", "01/10/2026 07:31:29", "", "0"])
    assert (r.numero, r.fecha, r.vacia) == ("71", "01/10/2026 07:31:29", False)
    assert parse_row(["", "", "", "0", "  /  /       :  :  ", "", "0"]).vacia
    r = parse_row(["AMBULATORIO", "8", "01/10/2026 15:20:00", ""])
    assert (r.numero, r.tipo) == ("8", "AMBULATORIO")


def test_estado_por_titulos_en_orden_z():
    pila = ["Seleccionar Folio", "Rango de Impresion HC", "Ingresos de Pacientes", "Menú Gestión Asistencial",
            "MENU SISTEMA DE INFORMACION EN SALUD", "Menu Principal"]
    assert state_from_titles(pila) == "FOLIO"
    assert state_from_titles(pila[2:]) == "INGRESOS"
    assert state_from_titles(["Selección Cliclo de Proceso"]) == "CICLO"
    assert state_from_titles(["Selección Ciclo de Proceso"]) == "CICLO"
    assert state_from_titles(["Mensaje", "Rango de Impresion HC"]) == "MENSAJE"
    assert state_from_titles([]) is None


def test_item_por_texto_y_no_por_posicion():
    base = ["RANGO DE FECHAS", "RANGO DE FOLIOS", "INCAPACIDADES", "FORMULACION", "ORDENES MEDICAS"]
    extra = ["RANGO DE FECHAS", "RANGO DE FOLIOS", "ENFERMERIA", "INCAPACIDADES", "CURVAS", "FORMULACION  ",
             "ORDENES MEDICAS", "AIEPI"]
    assert base[find_item_index(base, "FORMULACION")] == "FORMULACION"
    assert extra[find_item_index(extra, "FORMULACION")].strip() == "FORMULACION"
    assert find_item_index(extra, "RANGO DE FOLIOS") == 1
    assert find_item_index(extra, "NO EXISTE") is None


def test_iconos_y_fila_inferior():
    assert is_icon_size(29, 29) and not is_icon_size(157, 31)
    rects = [(986, 475, 1015, 504), (1095, 640, 1124, 669), (1127, 640, 1156, 669)]
    assert bottom_row_by_left(rects) == [1, 2]
    dlg = [(536, 440, 565, 469), (568, 440, 597, 469), (600, 440, 629, 469)]
    assert bottom_row_by_left(dlg)[-2] == 1          # el ✓ es el penúltimo


def test_layout_con_tolerancia():
    rects = [(120, 460, 281, 497), (120, 425, 281, 456), (500, 300, 600, 400)]
    assert match_layout(rects, [123, 465, 280, 496], 12) == 0
    assert match_layout(rects, [10, 10, 20, 20], 12) is None


def test_month_matches():
    assert month_matches(["2026", "10", "OCTUBRE   "], 2026, 10)
    assert not month_matches(["2026", "9", "SEPTIEMBRE"], 2026, 10)
