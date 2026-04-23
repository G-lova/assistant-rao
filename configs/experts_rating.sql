WITH experts AS (
	SELECT 
		u.id AS expert_id,
		COUNT(CASE 
				WHEN ee.updated_at BETWEEN ? AND ?
				THEN 1 
			END) AS countTotalExpertises,
		SUM(CASE 
				WHEN ee.updated_at BETWEEN ? AND ?
				THEN COALESCE(ee.accept, 0)
			END) AS countAcceptedExpertises,
		COALESCE(SUM(CASE 
			WHEN ee.updated_at BETWEEN ? AND ?
			THEN COALESCE(ee.secondUpload, 0)
		END), 0) AS secondUpload,
		COALESCE(SUM(CASE WHEN ee.deleted_at BETWEEN ? AND ? 
			AND ee.uploadExpertDate IS NULL 
			AND ee.expert_id MEMBER OF(e.declineExperts)
			THEN 1
		END), 0) AS overdues,
		ROUND(AVG(CASE 
			WHEN ee.updated_at BETWEEN ? AND ?
			THEN ee.`range`
			ELSE NULL 
		END) * 100, 1) AS criterion4,
		ROUND(AVG(CASE 
			WHEN ee.updated_at BETWEEN ? AND ?
			THEN CASE 
				WHEN e.object IN (1,7) AND ee.accept = 1
				THEN CASE
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (3)) OR `checkType2` = 14 THEN 0.5
					WHEN ee.expertise_id IN (SELECT id FROM expertises WHERE `type` IN (1)) OR `checkType2` = 15 THEN 0.75
					ELSE ee.accept
				END	
				ELSE ee.accept
			END
			ELSE NULL
		END) * 100, 1) AS criterion5
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
			WHEN e.countAcceptedExpertises > 0 AND e.countAcceptedExpertises <= 5 THEN 25
			WHEN e.countAcceptedExpertises > 5 AND e.countAcceptedExpertises <= 10 THEN 50
			WHEN e.countAcceptedExpertises > 10 AND e.countAcceptedExpertises <= 15 THEN 75
			WHEN e.countAcceptedExpertises > 15 THEN 100
			ELSE NULL
		END, 1) AS criterion1,	
		ROUND(CASE 
			WHEN e.countTotalExpertises > 0
			THEN (e.countTotalExpertises - e.overdues) * 100 / e.countTotalExpertises
			ELSE NULL
		END, 1) AS criterion2,
		ROUND(CASE 
			WHEN e.countTotalExpertises > 0
			THEN 100
			ELSE NULL
		END, 1) AS criterion3
	FROM experts e 
)
SELECT 
	*,
	ROUND((0.2 * criterion1 + 0.1 * criterion2 + 0.1 * criterion3 + 0.4 * criterion4 + 0.2 * criterion5), 2) AS criterion_rating
FROM experts_rating;