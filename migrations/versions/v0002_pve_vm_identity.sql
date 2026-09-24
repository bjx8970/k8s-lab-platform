-- Immutable after publication: provider-qualified VM identity.
LOCK TABLE vms, clusters, pve_servers IN SHARE ROW EXCLUSIVE MODE;
ALTER TABLE vms ADD COLUMN IF NOT EXISTS pve_server_id INTEGER;

DO $migration$
DECLARE
    problem TEXT;
BEGIN
    SELECT string_agg(v.id::text, ', ' ORDER BY v.id) INTO problem
    FROM vms v LEFT JOIN clusters c ON c.id = v.cluster_id WHERE c.id IS NULL;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: VM without Cluster (row IDs: %)', problem;
    END IF;

    SELECT string_agg(v.id::text, ', ' ORDER BY v.id) INTO problem
    FROM vms v JOIN clusters c ON c.id = v.cluster_id
    WHERE c.pve_server_id IS NULL OR c.pve_server_id <= 0
       OR (v.pve_server_id IS NOT NULL AND v.pve_server_id <= 0);
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: invalid PVE server ID (VM row IDs: %)', problem;
    END IF;

    SELECT string_agg(v.id::text, ', ' ORDER BY v.id) INTO problem
    FROM vms v JOIN clusters c ON c.id = v.cluster_id
    WHERE v.pve_server_id IS NOT NULL AND v.pve_server_id <> c.pve_server_id;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: VM/Cluster PVE server mismatch (VM row IDs: %)', problem;
    END IF;

    SELECT string_agg(v.id::text, ', ' ORDER BY v.id) INTO problem
    FROM vms v JOIN clusters c ON c.id = v.cluster_id
    LEFT JOIN pve_servers s ON s.id = c.pve_server_id WHERE s.id IS NULL;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: PVE server missing (VM row IDs: %)', problem;
    END IF;

    SELECT string_agg(v.id::text, ', ' ORDER BY v.id) INTO problem
    FROM vms v WHERE v.vmid IS NULL OR v.vmid <= 0
       OR v.node IS NULL OR v.node !~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$'
       OR length(v.node) > 32;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: invalid VMID or node locator (VM row IDs: %)', problem;
    END IF;

    SELECT string_agg(rows.id::text, ', ' ORDER BY rows.id) INTO problem
    FROM (
        SELECT v.id, COUNT(*) OVER (PARTITION BY c.pve_server_id, v.vmid) AS identity_count
        FROM vms v JOIN clusters c ON c.id = v.cluster_id
    ) AS rows WHERE rows.identity_count > 1;
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'v0002: duplicate provider-qualified identity (VM row IDs: %)', problem;
    END IF;
END $migration$;

UPDATE vms AS v SET pve_server_id = c.pve_server_id FROM clusters AS c
WHERE c.id = v.cluster_id AND v.pve_server_id IS NULL;

DO $migration$
DECLARE
    old_constraint RECORD;
    old_index RECORD;
BEGIN
    FOR old_constraint IN
        SELECT con.conname FROM pg_constraint con
        WHERE con.conrelid = 'vms'::regclass AND con.contype = 'u'
          AND (SELECT array_agg(att.attname ORDER BY positions.ordinality)
               FROM unnest(con.conkey) WITH ORDINALITY AS positions(attnum, ordinality)
               JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = positions.attnum) = ARRAY['vmid']::name[]
    LOOP
        EXECUTE format('ALTER TABLE vms DROP CONSTRAINT %I', old_constraint.conname);
    END LOOP;
    FOR old_index IN
        SELECT idx.relname AS index_name FROM pg_index ix
        JOIN pg_class idx ON idx.oid = ix.indexrelid
        WHERE ix.indrelid = 'vms'::regclass AND ix.indisunique AND ix.indisvalid
          AND ix.indnkeyatts = 1 AND ix.indnatts = 1 AND ix.indexprs IS NULL
          AND ix.indpred IS NULL AND NOT EXISTS
              (SELECT 1 FROM pg_constraint con WHERE con.conindid = ix.indexrelid)
          AND (SELECT att.attname FROM pg_attribute att
               WHERE att.attrelid = ix.indrelid AND att.attnum = ix.indkey[0]) = 'vmid'
    LOOP
        EXECUTE format('DROP INDEX %I', old_index.index_name);
    END LOOP;
END $migration$;

ALTER TABLE clusters
    ADD CONSTRAINT uq_clusters_id_pve_server_id UNIQUE (id, pve_server_id);

ALTER TABLE vms
    ALTER COLUMN pve_server_id SET NOT NULL,
    ADD CONSTRAINT fk_vms_pve_server_id FOREIGN KEY (pve_server_id) REFERENCES pve_servers(id),
    ADD CONSTRAINT fk_vms_cluster_pve_server FOREIGN KEY (cluster_id, pve_server_id)
        REFERENCES clusters(id, pve_server_id),
    ADD CONSTRAINT ck_vms_pve_server_id_positive CHECK (pve_server_id > 0),
    ADD CONSTRAINT ck_vms_vmid_positive CHECK (vmid > 0),
    ADD CONSTRAINT uq_vms_pve_server_vmid UNIQUE (pve_server_id, vmid);
CREATE INDEX ix_vms_pve_server_id ON vms (pve_server_id);
