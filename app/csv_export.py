"""CSV dialect shared by administrator downloads and archived tables."""

import csv


def excel_csv_writer(stream):
    # UTF-8 BOM lets Excel recognise Cyrillic; semicolon matches the owner's list separator.
    # Keep the header as the first record, without an Excel-only `sep=` pseudo-row.
    stream.write("\ufeff")
    return csv.writer(stream, delimiter=";", lineterminator="\r\n")
