/** Dependency-free JSDoc mirror of `ouroboros.gateway.skill_source_contracts`: skill sources, the catalog
 *  of every connected source, install, update, uninstall and the onboarding starter list. */

/**
 * One source with its read state (`kind`: builtin | hub | git | local | registry; `trust`: trusted | review).
 * @typedef {Object} SkillSourceRow
 * @property {string} id
 * @property {string} name
 * @property {string} kind
 * @property {string} trust
 * @property {boolean} confirmed
 * @property {string=} url
 * @property {string=} ref
 * @property {string=} commit
 * @property {string=} path
 * @property {string=} part  // official | community (a hub's half)
 * @property {string=} state  // ready | reading | error | search
 * @property {string=} last_read_at
 * @property {string=} error
 * @property {boolean=} refreshing
 */

/**
 * `GET /api/skill-sources`.
 * @typedef {Object} SkillSourcesResponse
 * @property {SkillSourceRow[]} sources
 * @property {boolean} materialized
 * @property {string} problem
 */

/**
 * `POST /api/skill-sources/probe`.
 * @typedef {Object} SkillSourceProbeRequest
 * @property {string=} url
 * @property {string=} path
 */

/**
 * @typedef {Object} SkillSourceProbeResponse
 * @property {string=} kind
 * @property {string=} suggested_name
 * @property {string[]=} found
 * @property {string=} error
 */

/**
 * `POST /api/skill-sources`.
 * @typedef {Object} SkillSourceAddRequest
 * @property {string=} url
 * @property {string=} path
 * @property {string} name
 * @property {string} trust
 */

/**
 * `POST /api/skill-sources/{source_id}`.
 * @typedef {Object} SkillSourceChangeRequest
 * @property {string=} name
 * @property {string=} trust
 * @property {boolean=} confirmed
 */

/**
 * @typedef {Object} SkillSourceChangeResponse
 * @property {boolean} ok
 * @property {SkillSourceRow[]=} sources
 * @property {SkillSourceRow=} source
 * @property {string=} error
 * @property {string=} code
 */

/**
 * One skill a source offers; `id` is `<source_id>/<name>`.
 * @typedef {Object} SkillCatalogItem
 * @property {string} id
 * @property {string} source_id
 * @property {string} source_name
 * @property {string=} trust
 * @property {string} name
 * @property {string=} description
 * @property {string[]=} category
 * @property {string=} version
 * @property {string=} digest
 * @property {boolean=} official
 * @property {boolean=} recommended
 * @property {string=} owner
 * @property {string=} repo_url
 * @property {string=} author_repo
 * @property {?Object=} signals  // {votes, comments, discussion_url, author_repo_stars}; null = no data
 * @property {?Object=} withdrawn  // {reason, at}
 * @property {string=} type
 * @property {string[]=} needs_keys
 * @property {string[]=} permissions
 * @property {*=} install_specs
 * @property {?Object=} installed  // {state: installed|name_taken, ...}
 * @property {string=} registry_slug
 * @property {Object=} registry_stats
 */

/**
 * `GET /api/skill-catalog`.
 * @typedef {Object} SkillCatalogResponse
 * @property {SkillCatalogItem[]} items
 * @property {Object[]} categories
 * @property {SkillSourceRow[]} sources
 * @property {Object=} starter
 */

/**
 * `GET /api/skill-catalog/search`.
 * @typedef {Object} SkillCatalogSearchResponse
 * @property {SkillCatalogItem[]} items
 * @property {string=} registry_error
 */

/**
 * `GET /api/skill-catalog/item/{source_id}/{name}`.
 * @typedef {Object} SkillCatalogItemDetailResponse
 * @property {SkillCatalogItem} item
 * @property {string=} about
 * @property {string=} about_error
 * @property {Object[]=} uses
 * @property {Object[]=} versions
 * @property {Object=} links
 */

/**
 * `POST /api/skill-catalog/install`.
 * @typedef {Object} SkillCatalogInstallRequest
 * @property {string} id
 * @property {boolean=} turn_on_when_ready
 * @property {boolean=} replace
 */

/**
 * @typedef {Object} SkillCatalogInstallResponse
 * @property {boolean} ok
 * @property {string=} job_id
 * @property {string=} name
 * @property {string=} basis
 * @property {string=} review_job_id
 * @property {boolean=} already_installed
 * @property {string=} code
 * @property {string=} error
 * @property {Object=} occupant
 */

/**
 * `POST /api/skills/{skill}/update`.
 * @typedef {Object} SkillUpdateRequest
 * @property {string} mode  // replace | keep | merge
 * @property {string=} digest
 * @property {string=} local_digest
 */

/**
 * @typedef {Object} SkillUpdateResponse
 * @property {boolean} ok
 * @property {string=} name
 * @property {string=} mode
 * @property {string=} job_id
 * @property {Object=} skipped
 * @property {boolean=} base_available
 * @property {string=} task
 * @property {Object=} candidate
 * @property {string=} local_digest
 * @property {string=} code
 * @property {string=} error
 */

/**
 * `POST /api/skills/{skill}/uninstall`.
 * @typedef {Object} SkillUninstallResponse
 * @property {boolean} ok
 * @property {string=} name
 * @property {string=} source
 * @property {string=} code
 * @property {string=} error
 */

/**
 * `GET /api/onboarding/starter-skills`.
 * @typedef {Object} StarterSkillsResponse
 * @property {Object[]} groups
 * @property {Object[]} hub_rows
 * @property {SkillSourceRow[]} sources
 */
