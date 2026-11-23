"""Built-in entities: the target fields `syncline map` knows how to look for.

Each field lists the names it commonly appears under in HR exports, in English and
French. Matching is done on normalized text (no accents, no punctuation, lowercase),
so "Date d'entrée", "DATE_ENTREE" and "date entree" are the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from syncline.spec import FieldType, TransformName


@dataclass(frozen=True)
class EntityField:
    name: str
    type: FieldType = "string"
    synonyms: tuple[str, ...] = ()
    required: bool = False
    unique: bool = False
    transform: tuple[TransformName, ...] = ("trim",)
    describe_values: bool = False  # list the distinct values seen, to help write a value map


@dataclass(frozen=True)
class Entity:
    name: str
    table: str
    key: str
    fields: tuple[EntityField, ...] = field(default_factory=tuple)

    def get(self, name: str) -> EntityField | None:
        return next((f for f in self.fields if f.name == name), None)


USER = Entity(
    name="user",
    table="users",
    key="external_id",
    fields=(
        EntityField(
            "external_id",
            synonyms=(
                "matricule",
                "employee id",
                "employee number",
                "emp id",
                "staff id",
                "personnel number",
                "id salarie",
                "numero salarie",
                "badge",
                "id",
            ),
            required=True,
            unique=True,
        ),
        EntityField(
            "first_name",
            synonyms=("prenom", "first name", "firstname", "given name", "forename"),
            required=True,
            transform=("trim", "collapse_spaces", "title"),
        ),
        EntityField(
            "last_name",
            synonyms=("nom", "last name", "lastname", "surname", "family name", "nom de famille"),
            required=True,
            transform=("trim", "collapse_spaces", "title"),
        ),
        EntityField(
            "email",
            type="email",
            synonyms=(
                "e mail",
                "mail",
                "courriel",
                "email address",
                "adresse email",
                "adresse mail",
                "work email",
                "email professionnel",
            ),
            required=True,
            unique=True,
            transform=("trim", "lower"),
        ),
        EntityField(
            "department",
            synonyms=(
                "service",
                "departement",
                "dept",
                "team",
                "equipe",
                "division",
                "direction",
                "business unit",
            ),
            describe_values=True,
        ),
        EntityField(
            "job_title",
            synonyms=(
                "poste",
                "fonction",
                "title",
                "job",
                "position",
                "intitule de poste",
                "job title",
                "emploi",
            ),
        ),
        EntityField(
            "hire_date",
            type="date",
            synonyms=(
                "date entree",
                "date d entree",
                "date embauche",
                "date d embauche",
                "start date",
                "hire date",
                "entry date",
                "date arrivee",
            ),
            transform=(),
        ),
        EntityField(
            "end_date",
            type="date",
            synonyms=(
                "date sortie",
                "date de sortie",
                "date depart",
                "termination date",
                "end date",
                "leaving date",
                "date fin contrat",
            ),
            transform=(),
        ),
        EntityField(
            "manager_id",
            synonyms=(
                "manager",
                "matricule manager",
                "responsable",
                "n 1",
                "supervisor",
                "manager id",
                "line manager",
            ),
        ),
        EntityField(
            "phone",
            synonyms=(
                "telephone",
                "tel",
                "mobile",
                "portable",
                "phone number",
                "numero de telephone",
            ),
            transform=("digits_only",),
        ),
    ),
)

ENTITIES = {USER.name: USER}


def custom_entity(name: str, field_names: list[str]) -> Entity:
    """An entity from a plain list of field names; the first one is the key."""
    if not field_names:
        raise ValueError("give at least one field name")
    fields = tuple(
        EntityField(n, required=i == 0, unique=i == 0) for i, n in enumerate(field_names)
    )
    return Entity(name=name, table=f"{name}s", key=field_names[0], fields=fields)
