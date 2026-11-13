import re
from datetime import datetime, time
from django import forms
from django.utils import timezone
from .models import ReferenceEntry
from .procurement_groups import grouped_choices
from .company_choices import TAXPAYER_TYPES


def split_terms(value, phrase=False):
    terms = list(dict.fromkeys(' '.join(t.split()) for t in re.split(r"[\n\r\t;,]+" if phrase else r"[\s;,]+", value or "") if t.strip()))
    if any(len(t) > 150 for t in terms):
        raise forms.ValidationError("Тег не должен быть длиннее 150 символов.")
    return terms


class ProfileNameForm(forms.Form):
    name = forms.CharField(max_length=200, error_messages={
        'required': 'Введите название подборки.',
        'max_length': 'Название должно быть не длиннее 200 символов.',
    })


class SearchForm(forms.Form):
    terms = forms.CharField(label="Ключевые слова", required=False,
                            widget=forms.Textarea(attrs={"rows": 2, "placeholder": "телефон\nсмартфон"}),
                            help_text="Один тег — одно слово. Количество тегов не ограничено.")
    mode = forms.ChoiceField(label="Объединение условий", initial="any", choices=[("any", "Любое условие — ИЛИ"), ("all", "Все условия — И")])
    kind = forms.ChoiceField(label="Совпадение", choices=[("contains", "Ключевое слово"),("phrase","Ключевая фраза")], initial="contains")
    exclude = forms.CharField(label="Исключить слова", required=False,
                              widget=forms.Textarea(attrs={"rows": 2, "placeholder": "ремонт; монтаж"}),
                              help_text="Исключаем вхождения текста в названии и описании: услуг → услуги, ремонт → ремонта.")
    amount_min = forms.DecimalField(label="Сумма от, ₸", required=False, min_value=0, max_digits=18, decimal_places=2)
    amount_max = forms.DecimalField(label="Сумма до, ₸", required=False, min_value=0, max_digits=18, decimal_places=2)
    only_advance = forms.BooleanField(label="Только с авансом", required=False, initial=False)
    in_documents = forms.BooleanField(label="Искать также в техспецификациях", required=False)
    only_medical = forms.BooleanField(label="Закупки по медицине (med.ecc.kz, fms.ecc.kz)", required=False, initial=False)
    subject = forms.ChoiceField(label="Предмет закупки", required=False, initial="",
        choices=[("", "Все"), ("1", "Товары"), ("2", "Работы"), ("3", "Услуги")], widget=forms.RadioSelect)
    remaining_hours = forms.ChoiceField(label="До окончания приёма", required=False, initial="", choices=[("", "Все сроки"), ("48", "До 48 часов"), ("24", "До 24 часов")], widget=forms.RadioSelect)
    deadline_from = forms.DateField(label="Приём заявок до — начиная с", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    deadline_to = forms.DateField(label="Приём заявок до — по", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    status = forms.MultipleChoiceField(label="Статус лота", required=False, widget=forms.CheckboxSelectMultiple)
    method = forms.MultipleChoiceField(label="Способ закупки", required=False, widget=forms.CheckboxSelectMultiple)
    region = forms.MultipleChoiceField(label="Область поставки", required=False, widget=forms.CheckboxSelectMultiple)
    city = forms.MultipleChoiceField(label="Город", required=False, widget=forms.CheckboxSelectMultiple)
    customer_bin = forms.RegexField(label="БИН заказчика", regex=r"^[0-9]{12}$", required=False,
                                    error_messages={"invalid": "БИН должен содержать 12 цифр."})
    number = forms.CharField(label="Номер лота или объявления", required=False, max_length=100)
    number_kind = forms.ChoiceField(label="Искать номер", choices=[("lot_number", "Лота"), ("announcement_number", "Объявления")])


    def __init__(self, *args, **kwargs):
        bound=args[0] if args else kwargs.get('data')
        if bound is not None:
            values=bound.copy()
            if not values.get('kind') or values.get('kind') in ('word','field'):values['kind']='contains'
            if args:args=(values,*args[1:])
            else:kwargs['data']=values
        super().__init__(*args, **kwargs)
        if self.initial.get('kind') not in ('contains','phrase'):self.initial['kind']='contains'
        self.procurement_options = {}
        for field, kind in [("status", "lot-statuses"), ("method", "purchase-methods")]:
            rows = list(ReferenceEntry.objects.filter(kind=kind))
            groups = grouped_choices(rows, field)
            self.procurement_options[field] = groups
            self.fields[field].choices = [option for _, options in groups for option in options]
            if not self.initial.get(field) and not self.initial.get("_defaults_version"):
                self.initial[field] = ([f'open:{value}' for value in (6,7,9) if any(row.source_id == value for row in rows)] if field == 'status'
                                       else [str(row.source_id) for row in rows if row.name == 'Запрос ценовых предложений'])
        self.fields["region"].choices = [(str(r.source_id), r.name) for r in ReferenceEntry.objects.filter(kind="katos")]
        self.fields["city"].choices = [(str(r.source_id), r.name) for r in ReferenceEntry.objects.filter(kind="cities")]
        for field in ("region", "city", "status", "method"):
            value = self.initial.get(field)
            if isinstance(value, str):
                self.initial[field] = value.split("__") if value else []

        for field in ('status','method'):
            selected = set(str(value) for value in self[field].value() or [])
            groups = [{'label':label,'options':[{'value':value,'label':label_text,'selected':value in selected} for value,label_text in options]}
                      for label,options in self.procurement_options[field]]
            setattr(self,field+'_groups',groups)


    def clean_terms(self):
        terms = split_terms(self.cleaned_data["terms"],phrase=self.data.get('kind')=='phrase')
        return terms

    def clean_exclude(self):
        return split_terms(self.cleaned_data.get("exclude"))

    def clean(self):
        data = super().clean()
        if not data.get('terms') and not data.get('number'):
            self.add_error('terms', 'Введите ключевое слово или номер лота/объявления.')
        for low, high in [("amount_min", "amount_max"), ("deadline_from", "deadline_to")]:
            if data.get(low) is not None and data.get(high) is not None and data[low] > data[high]:
                self.add_error(high, "Верхняя граница должна быть не меньше нижней.")
        return data

    def specification(self):
        data = self.cleaned_data
        filters = {}
        for field, param in [("amount_min", "total_price__gte"), ("amount_max", "total_price__lte"),
                             ("deadline_from", "offer_end_date__gte"), ("deadline_to", "offer_end_date__lte")]:
            if data.get(field) is not None and data.get(field) != "":
                if field == "deadline_to":
                    filters[param] = datetime.combine(data[field], time(23, 59, 59), tzinfo=timezone.get_current_timezone()).isoformat()
                else:
                    filters[param] = str(data[field])
        for field, param in [("status", "status_id__in"), ("method", "purchase_method_id__in")]:
            if data.get(field):
                filters[param] = "__".join(dict.fromkeys(value.rsplit(":",1)[-1] for value in data[field]))
        regions = data.get("region", [])
        cities = data.get("city", [])
        city_rows = list(ReferenceEntry.objects.filter(kind="cities", source_id__in=cities))
        covered = {str(r.raw.get("region_id")) for r in city_rows}
        addresses = cities + [r for r in regions if r not in covered]
        if addresses:
            filters["delivery_address_id__in"] = "__".join(dict.fromkeys(addresses))
        if data.get("number"):
            filters[data["number_kind"]] = data["number"].strip()
        if data.get("remaining_hours"):
            filters["remaining_hours"] = int(data["remaining_hours"])
        if data.get("only_advance"):
            filters["only_advance"] = True
        if data.get("in_documents"):
            filters["in_documents"] = True
        if data.get("only_medical"):
            filters["only_medical"] = True
        if data.get("subject"):
            filters["subject"] = data["subject"]
        return {"terms": data["terms"], "exclude": data["exclude"], "kind": data["kind"],
                "mode": data["mode"], "backend": "local_index", "pages": 10, "auto_pages": False, "extended_descriptions": True, "filters": filters,
                "customer_bin": data.get("customer_bin", ""), "regions": regions, "cities": cities, "defaults_version": 1, "status_selection": data.get("status", [])}


class CompanySettingsForm(forms.Form):
    def clean_company_bin(self):
        from .models import CompanySettings
        value = self.cleaned_data['company_bin']
        if value and CompanySettings.objects.filter(company_bin=value).exclude(owner=self.owner).exists():
            raise forms.ValidationError('Этот БИН уже используется другим аккаунтом.')
        return value

    is_vat_payer = forms.TypedChoiceField(label='Работаем с НДС', choices=[('True','Да, плательщик НДС'),('False','Нет, без НДС')], coerce=lambda value: value == 'True', initial=True)
    company_bin = forms.RegexField(label='БИН компании', regex=r'^[0-9]{12}$', required=False,
        max_length=12, min_length=12, widget=forms.TextInput(attrs={'inputmode':'numeric','autocomplete':'off','placeholder':'12 цифр'}),
        error_messages={'invalid':'БИН должен содержать ровно 12 цифр.'},
        help_text='Используется для сравнения с победителем лота в действующем протоколе итогов.')
    company_name = forms.CharField(label='Полное название организации',max_length=500,required=False)
    taxpayer_type = forms.ChoiceField(label='Тип налогоплательщика на Госзакупе',choices=TAXPAYER_TYPES,required=False)
    address_city = forms.CharField(label='Город / населённый пункт',max_length=200,required=False)
    address_street = forms.CharField(label='Улица',max_length=300,required=False)
    address_house = forms.CharField(label='Номер дома / корпус',max_length=50,required=False)
    address_office = forms.CharField(label='Офис / помещение',max_length=50,required=False,
        help_text='Необязательно. Укажите номер с буквой, если она есть: например, 25Б.')
    legal_address = forms.CharField(max_length=1000,required=False,widget=forms.HiddenInput)

    def clean(self):
        data=super().clean()
        if self._structured_address_posted and any(data.get('address_'+key) for key in ('city','street','house','office')):
            for key in ('city','street','house'):
                if not data.get('address_'+key):self.add_error('address_'+key,'Заполните поле адреса.')
            data['legal_address']=', '.join(data.get('address_'+key,'') for key in ('city','street','house','office') if data.get('address_'+key))
        return data

    bank_account = forms.RegexField(label='Банковский счёт (ИИК)',regex=r'^KZ[0-9A-Z]{18}$',max_length=20,required=False,
        error_messages={'invalid':'Укажите ИИК: KZ и ещё 18 букв или цифр.'})
    bank_name = forms.CharField(label='Название банка',max_length=200,required=False)
    bank_bic = forms.RegexField(label='БИК банка',regex=r'^[A-Z0-9]{8}(?:[A-Z0-9]{3})?$',max_length=11,required=False,
        error_messages={'invalid':'БИК должен содержать 8 или 11 латинских букв или цифр.'})
    contact_phone = forms.RegexField(label='Контактный телефон для заявки',regex=r'^\+?[0-9 ()-]{10,25}$',max_length=25,required=False,
        widget=forms.TextInput(attrs={'type':'tel','placeholder':'+7 (___) ___-__-__'}),
        error_messages={'invalid':'Укажите телефон: цифры, пробелы, скобки, дефис и необязательный + в начале.'})

    def __init__(self,*args,**kwargs):
        self.owner = kwargs.pop('owner', None)
        bound=args[0] if args else kwargs.get('data')
        self._structured_address_posted = bound is not None and any('address_'+key in bound for key in ('city','street','house','office'))
        if bound is not None:
            values=bound.copy()
            initial=kwargs.get('initial') or {}
            for key,value in initial.items():
                if key not in values:values[key]=str(value) if key=='is_vat_payer' else value
            if 'is_vat_payer' not in values:values['is_vat_payer']='True'
            for key in ('bank_account','bank_bic'):
                if key in values:values[key]=values[key].strip().replace(' ','').upper()
            if args:args=(values,*args[1:])
            else:kwargs['data']=values
        super().__init__(*args,**kwargs)
