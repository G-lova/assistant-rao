WITH expertise_info AS ( 
	SELECT
		e.id AS expertise_id,
		e.subjectContract,
		u.id AS user_expertise_id,
		u.name AS expertise_name,
		CASE 
		    WHEN u.name IS NOT NULL AND TRIM(u.name) != '' 
		    THEN LOWER(SUBSTRING_INDEX(TRIM(u.name), ' ', 1))
		    ELSE ''
		END AS expertise_surname,
		CASE 
		    WHEN u.director IS NOT NULL AND TRIM(u.director) != '' 
		    THEN LOWER(SUBSTRING_INDEX(TRIM(u.director), ' ', 1))
		    ELSE ''
		END AS expertise_director,
		u.organization AS expertise_organization,
		e.priceContract, 
		e.exucutorContract, 
		CASE e.examination
			WHEN 2 THEN 0
			ELSE e.examination
		END AS expertise_examination,
		CASE 
			WHEN e.`object` IN (1,7) THEN 'Результаты исполнения заключенных контрактов/договоров Минобрнауки России и подведомственных Минобрнауки России организаций на выполнение работ/оказание услуг/поставку товара'
			WHEN e.`object` IN (2) THEN 'Результаты исполнения заключенных Минобрнауки России и подведомственных Минобрнауки России организаций соглашений на предоставление субсидий в виде грантов (основная экспертиза и дополнительные проверки при необходимости)'
			WHEN e.`object` IN (3) THEN 'Проекты государственных заданий Минобрнауки России для подведомственных организаций (предварительная экспертиза)'
			WHEN e.`object` IN (4) THEN 'Результаты исполнения государственных заданий Минобрнауки России для подведомственных организаций'
			WHEN e.`object` IN (5,6) THEN 'Сведения и документы о закупочной деятельности подведомственных Минобрнауки России организаций'
		END AS expertise_direction,
		CASE e.checkType 
			WHEN 2 THEN 'Сведения и документы о закупочной деятельности организации (мониторинг закупок)'
		END AS expertise_monitoring,	
		CASE 
			WHEN e.`object` IN (1,7) THEN 'Результаты исполнения заключенных государственных контрактов/договоров'
			WHEN e.`object` IN (2) THEN 'Результаты исполнения соглашений на предоставление субсидий в виде грантов (основная экспертиза и дополнительные проверки при необходимости)'
			WHEN e.`object` IN (3) THEN 'Проекты государственных заданий (предварительная экспертиза)'
			WHEN e.`object` IN (4) THEN 'Результаты исполнения государственных заданий'
			WHEN e.`object` IN (5,6) THEN 'Закупочная деятельность'
		END AS experienceExpertise_direction,
		e.`type`,
		CASE 
			WHEN e.regionExpertise IS NULL THEN 'Экспертиза отчетов'
			ELSE e.regionExpertise
		END AS expertise_regionExpertise, 
		REGEXP_REPLACE(
			CONCAT_WS(' ', LOWER(e.subjectContract), 
				LOWER(e.directionContract), 
				LOWER(u.organization), 
				LOWER(u.okved_name)
			), '["\'«»]', '') AS expertise_text_feature, 
		CASE 
			WHEN u.region_id IS NULL 
			THEN 
				CASE CAST(SUBSTRING(u.inn, 1, 2) AS UNSIGNED)
					WHEN 86 THEN 81
					WHEN 87 THEN 82
					WHEN 89 THEN 83
					WHEN 90 THEN 84
					WHEN 91 THEN 85
					WHEN 92 THEN 86
					WHEN 93 THEN 87
					WHEN 94 THEN 88
					WHEN 95 THEN 89
					WHEN 97 THEN 77
					ELSE CAST(SUBSTRING(u.inn, 1, 2) AS UNSIGNED)
				END
			ELSE u.region_id
		END AS expertise_region_id 
	FROM expertises e
	JOIN users u
	ON e.user_id = u.id
	WHERE e.id = :expertise_id
), expertise_with_coords AS (
	SELECT
		*,
		CASE expertise_region_id
			WHEN 1 THEN 44.6089
			WHEN 2 THEN 54.7355
			WHEN 3 THEN 51.8335
			WHEN 4 THEN 51.9581
			WHEN 5 THEN 42.9831
			WHEN 6 THEN 43.1667
			WHEN 7 THEN 43.4853
			WHEN 8 THEN 46.3080
			WHEN 9 THEN 44.2269
			WHEN 10 THEN 61.7850
			WHEN 11 THEN 61.6688
			WHEN 12 THEN 56.6344
			WHEN 13 THEN 54.1870
			WHEN 14 THEN 62.0278
			WHEN 15 THEN 43.0241
			WHEN 16 THEN 55.7963
			WHEN 17 THEN 51.7191
			WHEN 18 THEN 56.8527
			WHEN 19 THEN 53.7224
			WHEN 20 THEN 43.3180
			WHEN 21 THEN 56.1439
			WHEN 22 THEN 53.3561
			WHEN 23 THEN 45.0355
			WHEN 24 THEN 56.0153
			WHEN 25 THEN 43.1332
			WHEN 26 THEN 45.0445
			WHEN 27 THEN 48.4827
			WHEN 28 THEN 50.2907
			WHEN 29 THEN 64.5473
			WHEN 30 THEN 46.3476
			WHEN 31 THEN 50.5974
			WHEN 32 THEN 53.2434
			WHEN 33 THEN 56.1290
			WHEN 34 THEN 48.7071
			WHEN 35 THEN 59.2205
			WHEN 36 THEN 51.6615
			WHEN 37 THEN 57.0004
			WHEN 38 THEN 52.2864
			WHEN 39 THEN 54.7104
			WHEN 40 THEN 54.5140
			WHEN 41 THEN 53.0376
			WHEN 42 THEN 55.3547
			WHEN 43 THEN 58.6036
			WHEN 44 THEN 57.7677
			WHEN 45 THEN 55.4410
			WHEN 46 THEN 51.7304
			WHEN 47 THEN 59.9391
			WHEN 48 THEN 52.6088
			WHEN 49 THEN 59.5682
			WHEN 50 THEN 55.7558
			WHEN 51 THEN 68.9707
			WHEN 52 THEN 56.3269
			WHEN 53 THEN 58.5228
			WHEN 54 THEN 55.0084
			WHEN 55 THEN 54.9893
			WHEN 56 THEN 51.7682
			WHEN 57 THEN 52.9703
			WHEN 58 THEN 53.1951
			WHEN 59 THEN 58.0105
			WHEN 60 THEN 57.8194
			WHEN 61 THEN 47.2225
			WHEN 62 THEN 54.6293
			WHEN 63 THEN 53.1959
			WHEN 64 THEN 51.5336
			WHEN 65 THEN 46.9591
			WHEN 66 THEN 56.8380
			WHEN 67 THEN 54.7826
			WHEN 68 THEN 52.7213
			WHEN 69 THEN 56.8587
			WHEN 70 THEN 56.4846
			WHEN 71 THEN 54.1931
			WHEN 72 THEN 57.1530
			WHEN 73 THEN 54.3142
			WHEN 74 THEN 55.1600
			WHEN 75 THEN 52.0339
			WHEN 76 THEN 57.6261
			WHEN 77 THEN 55.7558
			WHEN 78 THEN 59.9391
			WHEN 79 THEN 48.7947
			WHEN 80 THEN 67.6381
			WHEN 81 THEN 61.0032
			WHEN 82 THEN 64.7364
			WHEN 83 THEN 66.5299
			WHEN 84 THEN 47.8388
			WHEN 85 THEN 44.9521
			WHEN 86 THEN 44.6167
			WHEN 87 THEN 48.0159
			WHEN 88 THEN 48.5740
			WHEN 89 THEN 46.6354
		END AS expertise_lon,
		CASE expertise_region_id
			WHEN 1 THEN 40.1005
			WHEN 2 THEN 55.9917
			WHEN 3 THEN 107.5841
			WHEN 4 THEN 85.9603
			WHEN 5 THEN 47.5047
			WHEN 6 THEN 44.8167
			WHEN 7 THEN 43.6071
			WHEN 8 THEN 44.2558
			WHEN 9 THEN 42.0465
			WHEN 10 THEN 34.3469
			WHEN 11 THEN 50.8365
			WHEN 12 THEN 47.8998
			WHEN 13 THEN 45.1839
			WHEN 14 THEN 129.7315
			WHEN 15 THEN 44.6905
			WHEN 16 THEN 49.1089
			WHEN 17 THEN 94.4378
			WHEN 18 THEN 53.2115
			WHEN 19 THEN 91.4437
			WHEN 20 THEN 45.6982
			WHEN 21 THEN 47.2489
			WHEN 22 THEN 83.7636
			WHEN 23 THEN 38.9753
			WHEN 24 THEN 92.8932
			WHEN 25 THEN 131.9113
			WHEN 26 THEN 41.9691
			WHEN 27 THEN 135.0839
			WHEN 28 THEN 127.5272
			WHEN 29 THEN 40.5668
			WHEN 30 THEN 48.0302
			WHEN 31 THEN 36.5889
			WHEN 32 THEN 34.3642
			WHEN 33 THEN 40.4066
			WHEN 34 THEN 44.5169
			WHEN 35 THEN 39.8915
			WHEN 36 THEN 39.2003
			WHEN 37 THEN 40.9739
			WHEN 38 THEN 104.2807
			WHEN 39 THEN 20.4522
			WHEN 40 THEN 36.2614
			WHEN 41 THEN 158.6510
			WHEN 42 THEN 86.0873
			WHEN 43 THEN 49.6680
			WHEN 44 THEN 40.9264
			WHEN 45 THEN 65.3411
			WHEN 46 THEN 36.1926
			WHEN 47 THEN 30.3159
			WHEN 48 THEN 39.5992
			WHEN 49 THEN 150.8085
			WHEN 50 THEN 37.6173
			WHEN 51 THEN 33.0750
			WHEN 52 THEN 44.0059
			WHEN 53 THEN 31.2699
			WHEN 54 THEN 82.9357
			WHEN 55 THEN 73.3682
			WHEN 56 THEN 55.0974
			WHEN 57 THEN 36.0635
			WHEN 58 THEN 45.0183
			WHEN 59 THEN 56.2342
			WHEN 60 THEN 28.3318
			WHEN 61 THEN 39.7187
			WHEN 62 THEN 39.7396
			WHEN 63 THEN 50.1002
			WHEN 64 THEN 46.0343
			WHEN 65 THEN 142.7380
			WHEN 66 THEN 60.5975
			WHEN 67 THEN 32.0453
			WHEN 68 THEN 41.4527
			WHEN 69 THEN 35.9176
			WHEN 70 THEN 84.9476
			WHEN 71 THEN 37.6173
			WHEN 72 THEN 65.5343
			WHEN 73 THEN 48.4031
			WHEN 74 THEN 61.4006
			WHEN 75 THEN 113.4996
			WHEN 76 THEN 39.8845
			WHEN 77 THEN 37.6173
			WHEN 78 THEN 30.3159
			WHEN 79 THEN 132.9218
			WHEN 80 THEN 53.0069
			WHEN 81 THEN 69.0189
			WHEN 82 THEN 177.4835
			WHEN 83 THEN 66.6145
			WHEN 84 THEN 35.1396
			WHEN 85 THEN 34.1024
			WHEN 86 THEN 33.5254
			WHEN 87 THEN 37.8029
			WHEN 88 THEN 39.3078
			WHEN 89 THEN 32.6169
		END AS expertise_lat	
	FROM expertise_info
), experts AS (
	SELECT 
		u.id AS expert_id,
		u.name AS expert_name,
		CASE 
		    WHEN u.name IS NOT NULL AND TRIM(u.name) != '' 
		    THEN LOWER(SUBSTRING_INDEX(TRIM(u.name), ' ', 1))
		    ELSE ''
		END AS expert_surname,
		u.organization AS expert_organization,
		u.diplom AS expert_diplom, 
		(CASE WHEN u.email IS NOT NULL THEN 1 ELSE 0 END 
			+ CASE WHEN u.contactEmail IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.organization IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.snils IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.passport IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.datePassport IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.code IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.birthday IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.phone IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.`number` IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.bank IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.bik IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.correspondentNumber IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.innOplata IS NOT NULL THEN 1 ELSE 0 END
			+ CASE WHEN u.kpp IS NOT NULL THEN 1 ELSE 0 END) / 15 AS personal_block, 
		u.directions,
		CASE 
			WHEN u.regionExpertises IS NULL THEN CAST('["Экспертиза отчетов"]' AS JSON)
			ELSE u.regionExpertises
		END AS expert_regionExpertises,
		REGEXP_REPLACE(
			CONCAT_WS(' ', LOWER(t.name), 
				LOWER(et.name), 
				LOWER(u.qualification), 
				LOWER(u.speciality), 
				LOWER(u.branchScienceDegree), 
				LOWER(u.branchScienceAcademicTitle),
				LOWER(u.diplom), 
				LOWER(u.organization), 
				LOWER(u.`position`)
			), '["\'«»]', '') AS expert_text_feature,
		u.examination AS expert_examination,		
		CASE 
			WHEN u.region_id IS NULL 
			THEN 
				CASE CAST(SUBSTRING(inn, 1, 2) AS UNSIGNED)
					WHEN 86 THEN 81
					WHEN 87 THEN 82
					WHEN 89 THEN 83
					WHEN 90 THEN 84
					WHEN 91 THEN 85
					WHEN 92 THEN 86
					WHEN 93 THEN 87
					WHEN 94 THEN 88
					WHEN 95 THEN 89
					WHEN 97 THEN 77
					ELSE CAST(SUBSTRING(inn, 1, 2) AS UNSIGNED)
				END
			ELSE u.region_id
		END AS expert_region_id, 
		u.education,
		u.experience,
		COALESCE((YEAR(CURDATE()) - u.yearDegree), 0) AS degreeExperience, 
		COALESCE((YEAR(CURDATE()) - u.yearAcademicTitle), 0) AS academicTitleExperience, 
		CASE 
			WHEN u.publication = 1 
				OR (JSON_LENGTH(u.linkPublication) > 0 
				AND CAST(u.linkPublication AS JSON) != CAST('[null]' AS JSON))
			THEN 1
			ELSE 0
		END AS publication,
		CASE 
			WHEN CAST(u.linkPublication AS JSON) = CAST('[null]' AS JSON) 
			THEN 0
			ELSE JSON_LENGTH(u.linkPublication)
		END AS countPublications,
		CASE 
			WHEN u.monographs = 1 
				OR (JSON_LENGTH(u.linkMonographs) > 0 
				AND CAST(u.linkMonographs AS JSON) != CAST('[null]' AS JSON))
			THEN 1
			ELSE 0
		END AS monographs,
		CASE 
			WHEN CAST(u.linkMonographs AS JSON) = CAST('[null]' AS JSON) 
			THEN 0
			ELSE JSON_LENGTH(u.linkMonographs)
		END AS countMonographs,
		u.experienceExpertise,
		COALESCE(u.workExpertise, (SELECT value FROM settings WHERE `key` IN ('max_applications_per_expert'))) AS desiredWeekWorkload,
		COUNT(CASE WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE status IN (4,5)) THEN 1 END) AS countExpertise,
		COUNT(CASE 
				WHEN ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 1 YEAR) 
				THEN 1 
			END) AS countExpertises_lastYear,
		COALESCE(SUM(CASE 
			WHEN ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 1 YEAR) 
			THEN COALESCE(ee.secondUpload, 0)
		END), 0) AS secondUpload_lastYear,
		SUM(CASE WHEN ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 1 YEAR) THEN CASE 
			WHEN ee.uploadExpertDate >= e.dateStatus3 
			THEN CASE 
					WHEN e.dateStatus3 IS NOT NULL AND e.dateStatus3 >= e.dateStatus2 THEN 
						(DATEDIFF(ee.uploadExpertDate, DATE_ADD(e.dateStatus3, INTERVAL 1 DAY)) + 1) -
						(SELECT COUNT(*) FROM holidays h 
						 WHERE h.`date` BETWEEN DATE_ADD(e.dateStatus3, INTERVAL 1 DAY) AND ee.uploadExpertDate)
					WHEN e.dateStatus2 IS NOT NULL AND DATEDIFF(ee.uploadExpertDate, e.dateStatus2) > 0 THEN 
						(DATEDIFF(ee.uploadExpertDate, DATE_ADD(e.dateStatus2, INTERVAL 1 DAY)) + 1) -
						(SELECT COUNT(*) FROM holidays h 
						 WHERE h.`date` BETWEEN DATE_ADD(e.dateStatus2, INTERVAL 1 DAY) AND ee.uploadExpertDate)
					ELSE 0
				END
			WHEN ee.uploadExpertDate >= e.dateStatus2
			THEN CASE
				WHEN e.dateStatus2 IS NOT NULL AND DATEDIFF(ee.uploadExpertDate, e.dateStatus2) > 0 THEN 
					(DATEDIFF(ee.uploadExpertDate, e.dateStatus2) + 1) -
					(SELECT COUNT(*) FROM holidays h 
					 WHERE h.`date` BETWEEN e.dateStatus2 AND ee.uploadExpertDate)
				ELSE 0
			END		
			ELSE 0
		END
		ELSE 0 
	END > 3) AS overdues,
		COALESCE(AVG(CASE WHEN ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 1 YEAR)
			THEN CASE
				WHEN ee.status = 1
				THEN COALESCE(ee.`range`, 0) 
				ELSE COALESCE(ee.`range`, NULL)
			END
			ELSE NULL 
		END), 0) AS criterion4,
		COALESCE(AVG(CASE 
			WHEN ee.uploadExpertDate >= DATE_SUB(NOW(), INTERVAL 1 YEAR) 
			THEN CASE 
				WHEN e.object IN (1,7) 
				THEN CASE
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (3)) THEN 0.5
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (1)) THEN 0.75
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (2,4,5)) THEN 1
				END	
				ELSE 1
			END
			ELSE NULL
		END), 0) AS criterion5,
        SUM(CASE WHEN e.status IN (3) THEN 1 ELSE 0 END) AS currentWeekWorkload 
	FROM users u 
	LEFT JOIN expertise_experts ee 
	ON u.id = ee.expert_id 
	LEFT JOIN expertises e 
	ON e.id = ee.expertise_id 
	LEFT JOIN training t
	ON u.training_id = t.id
	LEFT JOIN enlarged_training et 
	ON u.enlargedTraining_id = et.id
	WHERE u.`role` = 'expert' 
	AND u.active = 1 
	AND u.status = 3 
	AND u.deleted_at IS NULL 
	AND u.inn != '' AND CAST(SUBSTRING(u.inn, 1, 2) AS UNSIGNED) != 0 AND LOWER(u.name) NOT LIKE '%тест%' AND LOWER(u.name) NOT LIKE '%test%' 
	AND u.workExpertise != 0
	GROUP BY u.id
), 
expert_declines AS (
	SELECT 
	    expert_id,
	    COUNT(*) AS expert_declines
	FROM (
	    SELECT 
	        d.id,
	        d.dateStatus2,
	        de.expert_id
	    FROM expertises d
	    JOIN JSON_TABLE(
	        d.declineExperts,
	        "$[*]" COLUMNS (
	            expert_id INT PATH "$"
	        )
	    ) de
	    WHERE d.dateStatus2 >= DATE_SUB(NOW(), INTERVAL 1 YEAR)
	) declines
	GROUP BY expert_id
),
expert_requests AS (
	SELECT 
	    expert_id,
	    COUNT(*) AS expert_requests
	FROM (
	    SELECT 
	        d.id,
	        d.dateStatus2,
	        de.expert_id
	    FROM expertises d
	    JOIN JSON_TABLE(
	        d.experts,
	        "$[*]" COLUMNS (
	            expert_id INT PATH "$"
	        )
	    ) de
	    WHERE d.status IN (2)
	) requests
	GROUP BY expert_id
),
experts_with_coords AS (
	SELECT
		e.*,
		CASE e.expert_region_id
			WHEN 1 THEN 44.6089
			WHEN 2 THEN 54.7355
			WHEN 3 THEN 51.8335
			WHEN 4 THEN 51.9581
			WHEN 5 THEN 42.9831
			WHEN 6 THEN 43.1667
			WHEN 7 THEN 43.4853
			WHEN 8 THEN 46.3080
			WHEN 9 THEN 44.2269
			WHEN 10 THEN 61.7850
			WHEN 11 THEN 61.6688
			WHEN 12 THEN 56.6344
			WHEN 13 THEN 54.1870
			WHEN 14 THEN 62.0278
			WHEN 15 THEN 43.0241
			WHEN 16 THEN 55.7963
			WHEN 17 THEN 51.7191
			WHEN 18 THEN 56.8527
			WHEN 19 THEN 53.7224
			WHEN 20 THEN 43.3180
			WHEN 21 THEN 56.1439
			WHEN 22 THEN 53.3561
			WHEN 23 THEN 45.0355
			WHEN 24 THEN 56.0153
			WHEN 25 THEN 43.1332
			WHEN 26 THEN 45.0445
			WHEN 27 THEN 48.4827
			WHEN 28 THEN 50.2907
			WHEN 29 THEN 64.5473
			WHEN 30 THEN 46.3476
			WHEN 31 THEN 50.5974
			WHEN 32 THEN 53.2434
			WHEN 33 THEN 56.1290
			WHEN 34 THEN 48.7071
			WHEN 35 THEN 59.2205
			WHEN 36 THEN 51.6615
			WHEN 37 THEN 57.0004
			WHEN 38 THEN 52.2864
			WHEN 39 THEN 54.7104
			WHEN 40 THEN 54.5140
			WHEN 41 THEN 53.0376
			WHEN 42 THEN 55.3547
			WHEN 43 THEN 58.6036
			WHEN 44 THEN 57.7677
			WHEN 45 THEN 55.4410
			WHEN 46 THEN 51.7304
			WHEN 47 THEN 59.9391
			WHEN 48 THEN 52.6088
			WHEN 49 THEN 59.5682
			WHEN 50 THEN 55.7558
			WHEN 51 THEN 68.9707
			WHEN 52 THEN 56.3269
			WHEN 53 THEN 58.5228
			WHEN 54 THEN 55.0084
			WHEN 55 THEN 54.9893
			WHEN 56 THEN 51.7682
			WHEN 57 THEN 52.9703
			WHEN 58 THEN 53.1951
			WHEN 59 THEN 58.0105
			WHEN 60 THEN 57.8194
			WHEN 61 THEN 47.2225
			WHEN 62 THEN 54.6293
			WHEN 63 THEN 53.1959
			WHEN 64 THEN 51.5336
			WHEN 65 THEN 46.9591
			WHEN 66 THEN 56.8380
			WHEN 67 THEN 54.7826
			WHEN 68 THEN 52.7213
			WHEN 69 THEN 56.8587
			WHEN 70 THEN 56.4846
			WHEN 71 THEN 54.1931
			WHEN 72 THEN 57.1530
			WHEN 73 THEN 54.3142
			WHEN 74 THEN 55.1600
			WHEN 75 THEN 52.0339
			WHEN 76 THEN 57.6261
			WHEN 77 THEN 55.7558
			WHEN 78 THEN 59.9391
			WHEN 79 THEN 48.7947
			WHEN 80 THEN 67.6381
			WHEN 81 THEN 61.0032
			WHEN 82 THEN 64.7364
			WHEN 83 THEN 66.5299
			WHEN 84 THEN 47.8388
			WHEN 85 THEN 44.9521
			WHEN 86 THEN 44.6167
			WHEN 87 THEN 48.0159
			WHEN 88 THEN 48.5740
			WHEN 89 THEN 46.6354
		END AS expert_lon,
		CASE e.expert_region_id
			WHEN 1 THEN 40.1005
			WHEN 2 THEN 55.9917
			WHEN 3 THEN 107.5841
			WHEN 4 THEN 85.9603
			WHEN 5 THEN 47.5047
			WHEN 6 THEN 44.8167
			WHEN 7 THEN 43.6071
			WHEN 8 THEN 44.2558
			WHEN 9 THEN 42.0465
			WHEN 10 THEN 34.3469
			WHEN 11 THEN 50.8365
			WHEN 12 THEN 47.8998
			WHEN 13 THEN 45.1839
			WHEN 14 THEN 129.7315
			WHEN 15 THEN 44.6905
			WHEN 16 THEN 49.1089
			WHEN 17 THEN 94.4378
			WHEN 18 THEN 53.2115
			WHEN 19 THEN 91.4437
			WHEN 20 THEN 45.6982
			WHEN 21 THEN 47.2489
			WHEN 22 THEN 83.7636
			WHEN 23 THEN 38.9753
			WHEN 24 THEN 92.8932
			WHEN 25 THEN 131.9113
			WHEN 26 THEN 41.9691
			WHEN 27 THEN 135.0839
			WHEN 28 THEN 127.5272
			WHEN 29 THEN 40.5668
			WHEN 30 THEN 48.0302
			WHEN 31 THEN 36.5889
			WHEN 32 THEN 34.3642
			WHEN 33 THEN 40.4066
			WHEN 34 THEN 44.5169
			WHEN 35 THEN 39.8915
			WHEN 36 THEN 39.2003
			WHEN 37 THEN 40.9739
			WHEN 38 THEN 104.2807
			WHEN 39 THEN 20.4522
			WHEN 40 THEN 36.2614
			WHEN 41 THEN 158.6510
			WHEN 42 THEN 86.0873
			WHEN 43 THEN 49.6680
			WHEN 44 THEN 40.9264
			WHEN 45 THEN 65.3411
			WHEN 46 THEN 36.1926
			WHEN 47 THEN 30.3159
			WHEN 48 THEN 39.5992
			WHEN 49 THEN 150.8085
			WHEN 50 THEN 37.6173
			WHEN 51 THEN 33.0750
			WHEN 52 THEN 44.0059
			WHEN 53 THEN 31.2699
			WHEN 54 THEN 82.9357
			WHEN 55 THEN 73.3682
			WHEN 56 THEN 55.0974
			WHEN 57 THEN 36.0635
			WHEN 58 THEN 45.0183
			WHEN 59 THEN 56.2342
			WHEN 60 THEN 28.3318
			WHEN 61 THEN 39.7187
			WHEN 62 THEN 39.7396
			WHEN 63 THEN 50.1002
			WHEN 64 THEN 46.0343
			WHEN 65 THEN 142.7380
			WHEN 66 THEN 60.5975
			WHEN 67 THEN 32.0453
			WHEN 68 THEN 41.4527
			WHEN 69 THEN 35.9176
			WHEN 70 THEN 84.9476
			WHEN 71 THEN 37.6173
			WHEN 72 THEN 65.5343
			WHEN 73 THEN 48.4031
			WHEN 74 THEN 61.4006
			WHEN 75 THEN 113.4996
			WHEN 76 THEN 39.8845
			WHEN 77 THEN 37.6173
			WHEN 78 THEN 30.3159
			WHEN 79 THEN 132.9218
			WHEN 80 THEN 53.0069
			WHEN 81 THEN 69.0189
			WHEN 82 THEN 177.4835
			WHEN 83 THEN 66.6145
			WHEN 84 THEN 35.1396
			WHEN 85 THEN 34.1024
			WHEN 86 THEN 33.5254
			WHEN 87 THEN 37.8029
			WHEN 88 THEN 39.3078
			WHEN 89 THEN 32.6169
		END AS expert_lat,
		COALESCE(ed.expert_declines, 0) AS expert_declines,
		COALESCE(er.expert_requests, 0) + e.currentWeekWorkload AS currentWeekWorkloadRequests
	FROM experts e
	LEFT JOIN expert_declines ed
	ON ed.expert_id = e.expert_id
	LEFT JOIN expert_requests er
	ON er.expert_id = e.expert_id
), ee_joined AS ( 
	SELECT 
		ewc.*,
		u.*,
		CASE 
			WHEN JSON_CONTAINS(u.expert_regionExpertises, JSON_QUOTE(ewc.expertise_regionExpertise)) THEN 1
			ELSE 2
		END	AS regionExpertise_sort,
		6371 * 2 * ATAN2(SQRT(SIN(RADIANS(u.expert_lat - ewc.expertise_lat)/2) * SIN(RADIANS(u.expert_lat - ewc.expertise_lat)/2) + 
			COS(RADIANS(ewc.expertise_lat)) * COS(RADIANS(u.expert_lat)) * SIN(RADIANS(u.expert_lon - ewc.expertise_lon)/2) * 
			SIN(RADIANS(u.expert_lon - ewc.expertise_lon)/2)), SQRT(1-SIN(RADIANS(u.expert_lat - ewc.expertise_lat)/2) * SIN(RADIANS(u.expert_lat - ewc.expertise_lat)/2) + 
			COS(RADIANS(ewc.expertise_lat)) * COS(RADIANS(u.expert_lat)) * 
			SIN(RADIANS(u.expert_lon - ewc.expertise_lon)/2) * SIN(RADIANS(u.expert_lon - ewc.expertise_lon)/2))) AS region_distance_km,
		CASE 
			WHEN JSON_CONTAINS(u.experienceExpertise, JSON_QUOTE(ewc.experienceExpertise_direction)) THEN 1
			ELSE 0
		END AS 	experienceExpertise_rate,
		CAST(CASE 
			WHEN u.desiredWeekWorkload > 0 THEN u.desiredWeekWorkload - u.currentWeekWorkloadRequests
			ELSE 0
		END AS FLOAT) AS possibleWeekWorkload,
		CASE 
			WHEN MAX(u.countExpertise) OVER(PARTITION BY ewc.expertise_id) > 0
			THEN u.countExpertise / MAX(u.countExpertise) OVER(PARTITION BY ewc.expertise_id)
			ELSE 0
		END AS countExpertise_rate,
		CASE 
			WHEN u.countExpertises_lastYear > 0 AND u.countExpertises_lastYear <= 5 THEN 0.25
			WHEN u.countExpertises_lastYear > 5 AND u.countExpertises_lastYear <= 10 THEN 0.5
			WHEN u.countExpertises_lastYear > 10 AND u.countExpertises_lastYear <= 15 THEN 0.75
			WHEN u.countExpertises_lastYear > 15 THEN 1
			ELSE 0
		END AS criterion1,	
		CASE 
			WHEN u.countExpertises_lastYear > 0
			THEN 1 - u.overdues / u.countExpertises_lastYear
			ELSE 0
		END AS criterion2,
		CASE 
			WHEN u.countExpertises_lastYear > 0
			THEN 1 - u.secondUpload_lastYear / u.countExpertises_lastYear
			ELSE 0
		END AS criterion3,
		CASE 
			WHEN (u.countExpertises_lastYear + u.expert_declines) > 0
			THEN (u.countExpertises_lastYear) / (u.countExpertises_lastYear + u.expert_declines)
			ELSE 0
		END AS declines_rate,
		CASE 
			WHEN MAX(u.experience) OVER(PARTITION BY ewc.expertise_id) > 0
			THEN u.experience / MAX(u.experience) OVER(PARTITION BY ewc.expertise_id)	
			ELSE 0
		END AS experience_rate,
		CASE 
			WHEN MAX(u.academicTitleExperience) OVER(PARTITION BY ewc.expertise_id) > 0
			THEN u.academicTitleExperience / MAX(u.academicTitleExperience) OVER(PARTITION BY ewc.expertise_id)
			ELSE 0
		END AS academicTitleExperience_rate,
		CASE 
			WHEN MAX(u.degreeExperience) OVER(PARTITION BY ewc.expertise_id) > 0
			THEN u.degreeExperience / MAX(u.degreeExperience) OVER(PARTITION BY ewc.expertise_id)
			ELSE 0
		END AS degreeExperience_rate,
		CASE 
			WHEN MAX(u.education) OVER(PARTITION BY ewc.expertise_id) > 0
			THEN u.education / MAX(u.education) OVER(PARTITION BY ewc.expertise_id)
			ELSE 0
		END AS education_rate,
		CASE 
			WHEN MAX(u.publication + u.monographs) OVER(PARTITION BY ewc.expertise_id)
			THEN (u.publication + u.monographs) / MAX(u.publication + u.monographs) OVER(PARTITION BY ewc.expertise_id)
			ELSE 0
		END AS pubMon_rate,
		CASE
			WHEN MAX(u.countPublications + u.countMonographs) OVER(PARTITION BY ewc.expertise_id)
			THEN (u.countPublications + u.countMonographs) / MAX(u.countPublications + u.countMonographs) OVER(PARTITION BY ewc.expertise_id) 
			ELSE 0
		END AS countPubMon_rate
	FROM expertise_with_coords ewc
	LEFT JOIN experts_with_coords u
	ON (JSON_CONTAINS(u.directions, JSON_QUOTE(ewc.expertise_direction)) OR JSON_CONTAINS(u.directions, JSON_QUOTE(ewc.expertise_monitoring))) 
	AND (ewc.expertise_regionExpertise IN ('Экспертиза отчетов') OR JSON_CONTAINS(u.expert_regionExpertises, JSON_QUOTE(ewc.expertise_regionExpertise))) 
	AND ((ewc.expertise_examination = 1 AND ewc.expertise_examination = u.expert_examination) 
		OR ewc.expertise_examination IS NULL 
		OR ewc.expertise_examination != 1) 
)
SELECT 
	*,
	CASE 
		WHEN expertise_examination = 1 AND (MAX(region_distance_km) OVER(PARTITION BY expertise_id)) > 0
		THEN 1 - region_distance_km / MAX(region_distance_km) OVER(PARTITION BY expertise_id)
		ELSE 1
	END AS distance_rate,
	(0.2 * criterion1 + 0.1 * criterion2 + 0.1 * criterion3 + 0.4 * criterion4 + 0.2 * criterion5) AS criterion_rating,
	CAST((declines_rate + personal_block + education_rate + experience_rate 
	+ degreeExperience_rate	+ academicTitleExperience_rate + pubMon_rate 
	+ countPubMon_rate + experienceExpertise_rate + countExpertise_rate 
	) / 10 AS FLOAT) AS avg_rating
FROM ee_joined;