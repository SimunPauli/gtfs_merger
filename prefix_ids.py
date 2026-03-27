def find_conflicting_ids(feed, id_col, primary_table, identity_cols):
    df = getattr(feed, primary_table, None)
    if df is None:
        raise ValueError(f"Error: {primary_table} not found in feed")

    if any(col not in df.columns for col in identity_cols):
        raise ValueError(f"Error: one or more identity_cols not found in {primary_table}")

    use_identity_cols = identity_cols + [id_col]

    # Group by ID and count unique definitions
    id_definitions = (
        df
        .drop_duplicates(subset=use_identity_cols)
        .groupby(id_col)
        .size()
    )
    conflicting = set(id_definitions[id_definitions > 1].index)
    return conflicting


def apply_prefix_to_feed(feed, id_col, conflicting_ids, foreign_keys, primary_table):

    if not conflicting_ids:
        return

    # All tables/columns to update (primary + foreign keys)
    tables_to_update = [(primary_table, id_col)]
    tables_to_update.extend(foreign_keys)

    # Collect all conflicting IDs that exist in this feed
    feed_conflicting_ids = set()

    for table_name, col_name in tables_to_update:
        df = getattr(feed, table_name, None)
        if df is None or col_name not in df.columns:
            continue

        existing_ids = set(df[col_name].dropna().unique())
        feed_conflicting_ids.update(existing_ids & conflicting_ids)


    # Apply prefix to all tables
    for table_name, col_name in tables_to_update:
        df = getattr(feed, table_name, None)
        if df is None or col_name not in df.columns:
            continue

        mask = df[col_name].isin(feed_conflicting_ids)
        #add prefix from feed_id (feed_id was added to the combined_feed above)
        if mask.any():
            if "feed_id" not in df.columns:
                raise ValueError(f"{table_name} is missing feed_id")

            df.loc[mask, col_name] = (
                df.loc[mask, "feed_id"].astype(str)
                .str.replace("GTFS_", "", regex=False)
                .str.replace(".zip", "", regex=False)
                + "_"
                + df.loc[mask, col_name].astype(str)
            )

def apply_prefix_to_all_ids(feed, id_col, foreign_keys, primary_table):
    tables_to_update = [(primary_table, id_col), *foreign_keys]

    for table_name, col_name in tables_to_update:
        df = getattr(feed, table_name, None)
        if df is None:
            continue
        if col_name not in df.columns:
            continue
        if "feed_id" not in df.columns:
            raise ValueError(f"{table_name} is missing feed_id")
        prefix = (
            df["feed_id"].astype(str)
            .str.replace("GTFS_", "", regex=False)
            .str.replace(".zip", "", regex=False)
        )
        mask = df[col_name].notna()
        df.loc[mask, col_name] = (
            prefix[mask] + "_" + df.loc[mask, col_name].astype(str)
        )
    return feed