from config_loader import ENV_FILE, check_config, load_config


def main() -> int:
    try:
        config = load_config()
        errors, warnings = check_config(config)
        print("Project .env       :", ENV_FILE)
        print("User authentication:", "Managed per user in the Admin Portal")
        print("Base URL           :", config.base_url)
        print("Owner site         :", config.owner_site_code)
        print("DC document        :", config.dc_doc_code)
        print("Stock point        :", config.out_stock_point_code)
        print("Same-state scheme  :", config.doc_code_tn)
        print("Interstate scheme  :", config.doc_code_interstate)
        print("Discount factor    :", config.factor)
        print("Active site maps   :", len(config.sites))
        print("Posting/work date  :", config.work_date or "SOURCE DOCUMENT DATE")
        for warning in warnings:
            print("WARNING             :", warning)
        if errors:
            for error in errors:
                print("ERROR               :", error)
            return 1
        print("Configuration       : VALID")
        print("Ginesys connections : Use Test Connection in the portal for each user")
        return 0
    except Exception as exc:
        print("ERROR               :", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
