BEGIN TRANSACTION;
CREATE TABLE "Материалы" ("Название" TEXT, "Описание" TEXT);
INSERT INTO "Материалы" VALUES ('Памятка', 'Краткое описание');
COMMIT;
