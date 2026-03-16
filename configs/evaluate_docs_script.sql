SELECT
	e.id,
    u.organization,
    CASE e.object
        WHEN 1 THEN 'Результаты исполнения заключенных контрактов/договоров Минобрнауки России и подведомственных Минобрнауки России организаций'
        WHEN 2 THEN 'Результаты исполнения заключенных Минобрнауки России и подведомственных Минобрнауки России организаций соглашений на предоставление субсидий в виде грантов'
        WHEN 3 THEN 'Проекты государственных заданий Минобрнауки России для подведомственных организаций'
        WHEN 4 THEN 'Результаты исполнения государственных заданий Минобрнауки России для подведомственных организаций'
        WHEN 5 THEN 'Сведения и документы о закупочной деятельности подведомственных Минобрнауки России организаций'
        WHEN 6 THEN 'Документация о проведении закупки'
        WHEN 7 THEN 'Отчетные документы по контракту/договору'
        ELSE ''
    END as expertise_object,
    CASE e.type
        WHEN 4 THEN 'Федеральный закон "О контрактной системе в сфере закупок товаров, работ, услуг для обеспечения государственных и муниципальных нужд" от 05.04.2013 N 44-ФЗ'
        WHEN 5 THEN 'Федеральный закон "О закупках товаров, работ, услуг отдельными видами юридических лиц" от 18.07.2011 N 223-ФЗ'
        ELSE ''
    END AS law_reference,
    CASE 
        WHEN e.checkType2 IN (1,2,3) THEN 'Конкурс'
        WHEN e.checkType2 IN (4,5,6) THEN 'Аукцион'
        WHEN e.checkType2 IN (7,8) THEN 'Запрос котировок'
        WHEN e.checkType2 IN (9,10) THEN 'Запрос предложений'
        WHEN e.checkType2 IN (11) THEN 'Закупка у единственного поставщика (подрядчика, исполнителя)'
        WHEN e.checkType2 IN (12) THEN 'Иными способами, установленными положением о закупке и соответствующими требованиям части 3 настоящей статьи (ФЗ-223)'
        WHEN e.checkType2 IN (13) THEN 'Выполнение НИР'
        WHEN e.checkType2 IN (14) THEN 'Поставка товара'
        WHEN e.checkType2 IN (15) THEN 'Оказание услуг и выполнение работ'
        ELSE ''
    END AS procurement_method,
    CASE
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param1') = true THEN "Полный комплект документов о закупке"
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param2') = true THEN "Описание объекта закупки"
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param3') = true THEN "Обоснование начальной (максимальной) цены контракта"
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param4') = true THEN "Порядок оценки заявок участников закупки"
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param5') = true THEN "Приемка по Контракту еще не проводилась"
        WHEN JSON_EXTRACT(e.typeDopDetal, '$.param6') = true THEN "Приемка по Контракту завершена"
        ELSE ''
    END AS expertise_details,
    e.typeDopDetal,
    e.contract,
    e.dateContract,
    e.subjectContract,
    e.directionContract,
    e.priceContract,
    e.soFinancePriceContract,
    e.periodContract,
    e.exucutorContract,
    e.expertisePeriodContract,
    e.dopContract,
    e.dopFieldsContract,
    e.linkDocs,
    e.dopMaterials,
    e.docEmptyComment,
    doc_codes.d AS doc_code,
	CASE
		WHEN (e.`object` IN (1,2,4,7) AND e.checkType2 = 13 AND JSON_EXTRACT(e.typeDopDetal, '$.param5') = true
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractNIRFiles', 'docReportDoNIRFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.checkType2 = 13 AND JSON_EXTRACT(e.typeDopDetal, '$.param6') = true 
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractNIRFiles', 'docDocPriemActSdachFiles', 'docReportDoNIRFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.checkType2 = 14 AND JSON_EXTRACT(e.typeDopDetal, '$.param5') = true
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractPostTovarFiles', 'docCargoTaxFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.checkType2 = 14 AND JSON_EXTRACT(e.typeDopDetal, '$.param6') = true
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractPostTovarFiles', 'docPriemTovSchetFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.checkType2 = 15 AND JSON_EXTRACT(e.typeDopDetal, '$.param5') = true
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractDoWorkFiles', 'docValidAllIfFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.checkType2 = 15 AND JSON_EXTRACT(e.typeDopDetal, '$.param6') = true
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractDoWorkFiles', 'docDocPriemActSdachFiles', 'docValidAllIfFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND e.checkType2 = 11 AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND e.checkType2 = 11 AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docAssetSelOrgFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND e.checkType2 = 1 AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND e.checkType2 = 1 AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docAssetSelOrgFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND e.checkType2 IN (4,7) AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND e.checkType2 IN (4,7) AND (JSON_EXTRACT(e.typeDopDetal, '$.param1') = true OR types = CAST('["полный комплект"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docAssetSelOrgFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND e.checkType2 = 1 
            AND (JSON_EXTRACT(e.typeDopDetal, '$.param4') = true OR types = CAST('["порядок оценки заявок участников закупки"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND e.checkType2 = 1 
            AND (JSON_EXTRACT(e.typeDopDetal, '$.param4') = true OR types = CAST('["порядок оценки заявок участников закупки"]' AS JSON))
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docAssetSelOrgFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND (JSON_EXTRACT(e.typeDopDetal, '$.param2') = true OR types = CAST('["описание объекта закупки"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND (JSON_EXTRACT(e.typeDopDetal, '$.param2') = true OR types = CAST('["описание объекта закупки"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles', 'docAssetSelOrgFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 
            AND (JSON_EXTRACT(e.typeDopDetal, '$.param3') = true OR types = CAST('["обоснование начальной (максимальной) цены контракта/договора"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docMaterialValidNMCKFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 
            AND (JSON_EXTRACT(e.typeDopDetal, '$.param3') = true OR types = CAST('["обоснование начальной (максимальной) цены контракта/договора"]' AS JSON))
			AND doc_codes.d IN ('docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docMaterialValidNMCKFiles', 'docAssetSelOrgFiles'))
        OR (e.`object` IN (1,2,4,7) AND e.type = 2 AND (e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 0 OR e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') IS NULL)
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractNIRFiles', 'docReportDoNIRFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.type = 2 AND e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 1 
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractNIRFiles', 'docDocPriemActSdachFiles', 'docReportDoNIRFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.type = 3 AND (e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 0 OR e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') IS NULL)
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractPostTovarFiles', 'docCargoTaxFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.type = 3 AND e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 1 
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractPostTovarFiles', 'docPriemTovSchetFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.type = 1 AND (e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 0 OR e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') IS NULL)
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractDoWorkFiles', 'docValidAllIfFiles'))
		OR (e.`object` IN (1,2,4,7) AND e.type = 1 AND e.date > STR_TO_DATE((CASE
                WHEN JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')) LIKE '%-%'
                THEN SUBSTRING_INDEX(JSON_UNQUOTE(JSON_EXTRACT(e.periodContract, '$[0].date')), '-', -1)
                ELSE NULL
            END), '%d.%m.%Y') = 1 
			AND doc_codes.d IN ('contract', 'dateContract', 'subjectContract', 'linkDocs', 'docContractDoWorkFiles', 'docDocPriemActSdachFiles', 'docValidAllIfFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND types = CAST('["извещение"]' AS JSON) AND doc_codes.d IN ('docIzvejenieFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND types = CAST('["извещение"]' AS JSON) AND doc_codes.d IN ('docIzvejenieFiles', 'docAssetSelOrgFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND types = CAST('["извещение", "описание объекта закупки"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND types = CAST('["извещение", "описание объекта закупки"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docAssetSelOrgFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND types = CAST('["извещение", "обоснование начальной (максимальной) цены контракта/договора"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docMaterialValidNMCKFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND types = CAST('["извещение", "обоснование начальной (максимальной) цены контракта/договора"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docObosnNMCKFiles', 'docMaterialValidNMCKFiles', 'docAssetSelOrgFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 4 AND types = CAST('["извещение", "порядок оценки заявок участников закупки"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		OR (e.`object` IN (3,5,6) AND e.`type` = 5 AND types = CAST('["извещение", "порядок оценки заявок участников закупки"]' AS JSON)
			AND doc_codes.d IN ('docIzvejenieFiles', 'docProjContractFiles', 'docOpusObjectZacupFiles', 'docAssetSelOrgFiles', 'docPorViewOcenkFiles', 'docTrebContentRequestFiles'))
		THEN 1
		ELSE 0
	END AS required_docs,
    CASE 
    	WHEN doc_codes.d IN ('contract') AND e.contract IS NOT NULL THEN 1
    	WHEN doc_codes.d IN ('dateContract') AND e.dateContract IS NOT NULL THEN 1
    	WHEN doc_codes.d IN ('subjectContract') AND e.subjectContract IS NOT NULL THEN 1
    	WHEN doc_codes.d IN ('linkDocs') AND e.linkDocs IS NOT NULL THEN 1
		ELSE 0
    END AS provided_docs,
    CASE 
        WHEN LOWER(JSON_EXTRACT(e.docEmptyComment, CONCAT('$.', empty_comment.c))) IN ('null', '') THEN NULL
        ELSE JSON_EXTRACT(e.docEmptyComment, CONCAT('$.', empty_comment.c))
    END AS empty_comment,
    CASE 
    	WHEN doc_codes.d IN ('linkDocs') AND e.linkDocs IS NOT NULL THEN JSON_ARRAY(e.linkDocs)
    	WHEN CAST(JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', `keys`.k)) AS JSON) = CAST('[]' AS JSON) THEN NULL
        ELSE JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', `keys`.k)) 
    END AS links,
    CASE 
    	WHEN CAST(m.file_path AS JSON) = CAST('[]' AS JSON) THEN NULL
        ELSE m.file_path
	END AS file_path, 
    CASE
    	WHEN doc_codes.d IN ('linkDocs') AND e.linkDocs IS NOT NULL THEN JSON_ARRAY(e.linkDocs)
    	WHEN doc_codes.d IN ('docDopMaterialsFiles') AND e.dopMaterials IS NOT NULL AND e.dopMaterials NOT IN (CAST('[]' AS JSON), CAST('[null]' AS JSON)) THEN e.dopMaterials
        WHEN JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', `keys`.k)) IS NULL OR CAST(JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', `keys`.k)) AS JSON) = CAST('[]' AS JSON) THEN m.file_path
        WHEN m.file_path IS NULL OR CAST(m.file_path AS JSON) = CAST('[]' AS JSON) THEN JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', `keys`.k))
        ELSE JSON_MERGE_PRESERVE(
            JSON_EXTRACT(e.typeDopDetal, CONCAT('$.links.', keys.k)),
            JSON_EXTRACT(m.file_path, '$')        )
	END AS media_links
FROM expertises e
JOIN JSON_TABLE(
        CAST('["contract", "dateContract", "subjectContract", "linkDocs", "docProjContractFiles", "docContractNIRFiles", "docContractDoWorkFiles", "docContractPostTovarFiles",
				"docOpusObjectZacupFiles", "docObosnNMCKFiles", "docMaterialValidNMCKFiles", "docDocPriemActSdachFiles", "docPriemTovSchetFiles",
				"docValidAllIfFiles", "docCargoTaxFiles", "docReportDoNIRFiles", "docAssetSelOrgFiles", "docPorViewOcenkFiles", "docTrebContentRequestFiles",
				"docIzvejenieFiles", "docTechDocFiles", "docCertValidFiles", "docPhotoCargoFiles", "docValidCountyFiles", "docActPriemTovFiles",
				"docPhotoFinishWorkFiles", "docValidCopyriteFiles", "docValidGarantFiles", "docAcceptInafPostavFiles", "docDopConsentContractFiles",
				"docExpertReportFiles", "docVziskPenyFiles", "docDopMaterialsFiles", "unknown"]' AS JSON),
        '$[*]' COLUMNS (d VARCHAR(255) PATH '$')
     ) AS doc_codes
     ON 1=1
LEFT JOIN users u
ON u.id = e.user_id
LEFT JOIN JSON_TABLE(
        JSON_KEYS(JSON_EXTRACT(e.typeDopDetal, '$.links')),
        '$[*]' COLUMNS (k VARCHAR(255) PATH '$')
     ) AS `keys`
     ON `keys`.k = doc_codes.d
LEFT JOIN JSON_TABLE(
        JSON_KEYS(e.docEmptyComment),
        '$[*]' COLUMNS (c VARCHAR(255) PATH '$')
     ) AS empty_comment
     ON empty_comment.c = doc_codes.d
LEFT JOIN (SELECT model_id, collection_name, JSON_ARRAYAGG(CONCAT(?, id, '/', file_name)) AS file_path
           FROM media
     	   WHERE model_type LIKE '%Expertise'
           GROUP BY model_id, collection_name) m
     ON m.collection_name = `keys`.k
     AND m.model_id =e.id
WHERE e.id = ?;