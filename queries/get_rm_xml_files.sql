WITH ranked_xml AS (
    SELECT
        x.id AS current_xml_id,
        x.entry_name AS current_entry_name,
        x.eis_version AS current_version,
        x.eis_archive_id AS current_eis_archive_id,
        CONCAT(?, a.storage_path) AS current_archive_storage_path,
        x.risk_monitoring_contract_id,
        x.purchase_number,
        x.contract_number,
        SUBSTRING_INDEX(x.entry_name, '_', 2) AS xml_identifier,
        SUBSTRING_INDEX(x.entry_name, '_', 1) AS xml_tag,
        p.id AS previous_xml_id,
        p.entry_name AS previous_entry_name,
        p.eis_version AS previous_version,
        p.eis_archive_id AS previous_eis_archive_id,
        CONCAT(?, a2.storage_path) AS previous_archive_storage_path,
        ROW_NUMBER() OVER (
            PARTITION BY x.id
            ORDER BY
                p.eis_version DESC,
                p.created_at DESC,
                p.id DESC
        ) AS rn
    FROM risk_monitoring_eis_xml_sources x
    LEFT JOIN risk_monitoring_eis_archives a
        ON a.id = x.eis_archive_id
    LEFT JOIN risk_monitoring_eis_xml_sources p
        ON SUBSTRING_INDEX(p.entry_name, '_', 2)
           = SUBSTRING_INDEX(x.entry_name, '_', 2)
        AND SUBSTRING_INDEX(p.entry_name, '_', 1) IN (
            "epNotificationEZK2020",
            "epNotificationEF2020",
            "epNotificationEZT2020",
            "epNotificationEOK2020",
            "fcsNotificationEP",
            "fcsNotification111",
            "pprf615NotificationPO",
            "pprf615NotificationEF",
            "purchaseNotice",
            "purchaseNoticeOK",
            "purchaseNoticeOA",
            "purchaseNoticeAE",
            "purchaseNoticeAE94FZ",
            "purchaseNoticeAESMBO",
            "purchaseNoticeZK",
            "purchaseNoticeZKESMBO",
            "purchaseNoticeZPESMBO",
            "purchaseNoticeEP",
            "contract",
            "pprf615Contract",
            "contractCutted"
        )
        AND p.eis_version < x.eis_version
    LEFT JOIN risk_monitoring_eis_archives a2
        ON a2.id = p.eis_archive_id
    WHERE x.updated_at >= ? AND x.updated_at <= ?
)
SELECT
    r.current_xml_id,
    r.current_entry_name,
    r.current_version,
    r.current_eis_archive_id,
    r.current_archive_storage_path,
    r.risk_monitoring_contract_id,
    r.purchase_number,
    r.contract_number,
    r.xml_identifier,
    r.xml_tag,
    r.previous_xml_id,
    r.previous_entry_name,
    r.previous_version,
    r.previous_eis_archive_id,
    r.previous_archive_storage_path
FROM ranked_xml r
WHERE r.rn = 1
GROUP BY
    r.current_xml_id,
    r.current_entry_name,
    r.current_version,
    r.current_eis_archive_id,
    r.current_archive_storage_path,
    r.risk_monitoring_contract_id,
    r.purchase_number,
    r.contract_number,
    r.xml_identifier,
    r.xml_tag,
    r.previous_xml_id,
    r.previous_entry_name,
    r.previous_version,
    r.previous_eis_archive_id,
    r.previous_archive_storage_path
ORDER BY
    r.xml_identifier ASC,
    r.current_version ASC;