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
			AND ee.id NOT IN (
				SELECT id 
				FROM expertise_experts 
				WHERE (expert_id = 29 AND expertise_id IN (4248, 4313, 4339, 4340, 4344))
					OR (expert_id = 32 AND expertise_id IN (4184))
					OR (expert_id = 44 AND expertise_id IN (4139, 4152, 4256, 4257, 4281, 4283, 4289, 4349))
					OR (expert_id = 49 AND expertise_id IN (4156, 4179, 4205, 4343))
					OR (expert_id = 60 AND expertise_id IN (4069, 4145, 4152, 4200, 4258, 4271, 4277, 4279, 4290, 4291, 4293))
					OR (expert_id = 71 AND expertise_id IN (4157, 4158, 4180, 4185, 4263, 4264, 4275, 4286, 4311, 4325, 4331, 4355, 4358, 4360))
					OR (expert_id = 74 AND expertise_id IN (4198, 4280, 4301, 4313, 4339, 4347, 4360))
					OR (expert_id = 96 AND expertise_id IN (4208, 4305, 4342))
					OR (expert_id = 113 AND expertise_id IN (4298, 4337, 4354))
					OR (expert_id = 130 AND expertise_id IN (4183, 4189, 4331, 4343, 4345))
					OR (expert_id = 138 AND expertise_id IN (4187, 4195, 4323, 4340))
					OR (expert_id = 144 AND expertise_id IN (4195))
					OR (expert_id = 151 AND expertise_id IN (4147, 4252, 4290, 4306, 4310, 4348))
					OR (expert_id = 161 AND expertise_id IN (4078, 4141, 4144, 4145, 4148, 4149, 4249, 4262, 4267, 4268, 4276, 4277))
					OR (expert_id = 175 AND expertise_id IN (4177, 4288, 4297, 4303, 4317, 4318, 4321, 4346, 4347, 4351, 4352, 4362, 4381))
					OR (expert_id = 189 AND expertise_id IN (4178, 4260, 4267, 4333, 4352, 4359, 4363, 4379))
					OR (expert_id = 212 AND expertise_id IN (4080, 4153, 4157, 4210, 4264, 4285, 4286, 4287, 4296, 4324, 4341, 4376))
					OR (expert_id = 213 AND expertise_id IN (4203, 4258, 4261, 4293, 4294, 4295, 4306, 4315, 4320))
					OR (expert_id = 233 AND expertise_id IN (4209, 4247, 4257, 4310, 4316))
					OR (expert_id = 258 AND expertise_id IN (4311))
					OR (expert_id = 261 AND expertise_id IN (4196, 4253, 4254, 4348, 4355))
					OR (expert_id = 311 AND expertise_id IN (4279, 4291, 4316, 4326, 4332))
					OR (expert_id = 325 AND expertise_id IN (4068, 4070, 4071, 4073, 4074, 4078, 4184, 4255, 4283, 4302, 4319, 4327, 4361, 4365))
					OR (expert_id = 344 AND expertise_id IN (4146, 4259, 4262, 4301))
					OR (expert_id = 357 AND expertise_id IN (4075, 4142, 4148, 4149, 4151, 4206, 4212, 4213, 4272))
					OR (expert_id = 365 AND expertise_id IN (4147, 4150, 4205, 4266, 4269, 4298, 4299, 4346))
					OR (expert_id = 427 AND expertise_id IN (4153, 4154, 4178, 4185, 4186, 4191, 4192, 4197, 4274, 4282, 4287, 4288, 4289, 4296, 4307, 4322, 4335, 4342, 4361))
					OR (expert_id = 430 AND expertise_id IN (4265))
					OR (expert_id = 504 AND expertise_id IN (4155, 4191, 4247, 4253, 4269, 4270, 4271, 4284))
					OR (expert_id = 505 AND expertise_id IN (4077, 4079, 4140, 4144, 4270, 4276, 4281, 4292, 4299, 4307, 4309, 4314, 4328, 4378))
					OR (expert_id = 532 AND expertise_id IN (4326, 4351))
					OR (expert_id = 533 AND expertise_id IN (4071, 4183, 4202, 4207, 4265, 4308, 4324, 4330, 4333, 4358, 4377))
					OR (expert_id = 551 AND expertise_id IN (4068, 4070, 4073, 4075, 4077, 4079, 4080, 4141, 4177, 4252, 4297, 4302, 4318, 4319, 4320, 4322, 4328, 4362, 4365, 4378, 4381))
					OR (expert_id = 586 AND expertise_id IN (4312, 4338, 4377))
					OR (expert_id = 573 AND expertise_id IN (4193, 4200, 4202, 4210, 4259, 4266, 4268, 4272, 4284, 4292, 4359))
					OR (expert_id = 774 AND expertise_id IN (4182, 4196, 4204, 4211, 4248, 4249, 4261, 4263, 4273, 4321))
					OR (expert_id = 775 AND expertise_id IN (4280, 4327, 4337, 4354))
					OR (expert_id = 777 AND expertise_id IN (4187, 4208, 4295, 4317, 4356))
					OR (expert_id = 784 AND expertise_id IN (4146, 4199, 4308, 4336))
					OR (expert_id = 837 AND expertise_id IN (4198, 4209, 4254, 4255, 4278, 4282, 4332, 4349, 4357))
					OR (expert_id = 845 AND expertise_id IN (4140, 4181, 4193, 4203, 4294, 4309, 4314, 4357))
					OR (expert_id = 847 AND expertise_id IN (4069, 4072, 4074, 4142, 4150, 4197, 4278, 4285, 4300, 4315, 4345))
					OR (expert_id = 868 AND expertise_id IN (4330))
					OR (expert_id = 872 AND expertise_id IN (4151))
					OR (expert_id = 907 AND expertise_id IN (4206))
					OR (expert_id = 988 AND expertise_id IN (4179, 4180, 4181, 4192, 4194, 4204, 4211, 4212, 4251, 4274, 4275, 4304, 4305, 4325, 4329, 4335))
					OR (expert_id = 1540 AND expertise_id IN (4158, 4186, 4213, 4251, 4303, 4304, 4312, 4323, 4336, 4338, 4376))
					OR (expert_id = 1505 AND expertise_id IN (4072, 4139, 4154, 4155, 4182, 4194, 4273, 4300, 4341, 4344, 4350, 4356))
					OR (expert_id = 1590 AND expertise_id IN (4156, 4199, 4260, 4353, 4379))
					OR (expert_id = 1603 AND expertise_id IN (4189, 4207, 4329, 4353))
					OR (expert_id = 1613 AND expertise_id IN (4256, 4350, 4363))
			)
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