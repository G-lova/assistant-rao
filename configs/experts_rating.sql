WITH experts AS (
	SELECT 
		u.id AS expert_id,
		COUNT(CASE 
				WHEN ee.uploadExpertDate BETWEEN ? AND ?
				THEN 1 
			END) AS countExpertises_targetYear,
		COALESCE(SUM(CASE 
			WHEN ee.uploadExpertDate BETWEEN ? AND ?
			THEN COALESCE(ee.secondUpload, 0)
		END), 0) AS secondUpload_targetYear,
		SUM(CASE WHEN ee.uploadExpertDate BETWEEN ? AND ? THEN CASE 
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
		ROUND(COALESCE(AVG(CASE 
			WHEN ee.uploadExpertDate BETWEEN ? AND ? 
			THEN CASE
				WHEN ee.status = 1
				THEN COALESCE(ee.`range`, 0) 
				ELSE COALESCE(ee.`range`, NULL)
			END
			ELSE NULL 
		END), 0), 2) AS criterion4,
		ROUND(COALESCE(AVG(CASE 
			WHEN ee.uploadExpertDate BETWEEN ? AND ?
			THEN CASE 
				WHEN e.object IN (1,7) 
				THEN CASE
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (3)) OR `checkType2` = 14 THEN 0.5
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (1)) OR `checkType2` = 15 THEN 0.75
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (2,4,5)) THEN 1
				END	
				ELSE 1
			END
			ELSE NULL
		END), 0), 2) AS criterion5
	FROM users u 
	LEFT JOIN expertise_experts ee 
	ON u.id = ee.expert_id 
	LEFT JOIN expertises e 
	ON e.id = ee.expertise_id 
	WHERE u.`role` = 'expert' 
	GROUP BY u.id
), experts_rating AS ( 
	SELECT 
		e.*,
		ROUND(CASE 
			WHEN e.countExpertises_targetYear > 0 AND e.countExpertises_targetYear <= 5 THEN 0.25
			WHEN e.countExpertises_targetYear > 5 AND e.countExpertises_targetYear <= 10 THEN 0.5
			WHEN e.countExpertises_targetYear > 10 AND e.countExpertises_targetYear <= 15 THEN 0.75
			WHEN e.countExpertises_targetYear > 15 THEN 1
			ELSE 0
		END, 2) AS criterion1,	
		ROUND(CASE 
			WHEN e.countExpertises_targetYear > 0
			THEN 1 - e.overdues / e.countExpertises_targetYear
			ELSE 0
		END, 2) AS criterion2,
		ROUND(CASE 
			WHEN e.countExpertises_targetYear > 0
			THEN 1 - e.secondUpload_targetYear / e.countExpertises_targetYear
			ELSE 0
		END, 2) AS criterion3
	FROM experts e 
)
SELECT 
	*,
	expert_id,
	ROUND((0.2 * criterion1 + 0.1 * criterion2 + 0.1 * criterion3 + 0.4 * criterion4 + 0.2 * criterion5) * 100, 2) AS criterion_rating
FROM experts_rating;