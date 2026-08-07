## Query

```
with base as (
	Select identifiers_sessionid
	, action , label
	,identifiers_page_name , identifiers_log_time
	, service_line 
	, internalemployee
	, service_line
	-- Select  service_line, identifiers_page_category_2, count(*) cnt
	FROM 
	eshop_data.es_events_v2 
	where nc = 'de'
	AND internalemployee = 'no'
	AND identifiers_page_name like 'checkout%'
	and category like 'acquisition'
	and date BETWEEN date_add('day', - 7 , CURRENT_DATE) and  date_add('day', - 1 , CURRENT_DATE)  
	-- AND lower(category) <> 'addonmanagement'
	AND (
            'All' IN ({{service_line}})
            OR lower(service_line) = lower({{service_line}})
            OR lower(identifiers_page_category_2) = lower({{service_line}})
        )
	-- GROUP by 1,2 
	-- order by cnt desc
)
Select * from base where lower(action) like '%view%' and identifiers_page_name like 'checkout/appointment';
;
,intd as
(
SELECT 
	identifiers_sessionid, 
	-- max( case when  )
	min(case when action = 'PageView' AND identifiers_page_name = 'checkout/appointment' then identifiers_log_time end ) first_appointment_page_view ,
	MAX(case when action = 'PageView' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS appointment_page,
	MAX(case when action = 'clickInteractions' and label = 'date picked' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS date_picker_click,
	MAX(case when action = 'datePickerViewed' and label = 'bookingAppointmentDate' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS date_picker_view,
	MAX(case when action = 'dateSelected' and label = 'bookingAppointmentDate' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS date_selected,
	MAX(case when action = 'radioButtonClicks' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS time_slot,

	max(case when  
			(
				(action = 'clickInteractions' and label = 'date picked' )
				or 
				(action = 'clickInteractions' and label = 'date picked')
				or 
				(action = 'datePickerViewed' and label = 'bookingAppointmentDate')
				or
				(action = 'dateSelected' and label = 'bookingAppointmentDate' )
				or
				(action = 'radioButtonClicks')
			) AND identifiers_page_name = 'checkout/appointment'
		then 1 else 0 end 
			) any_interaction_done,
	max(case when action = 'popupAppears' and label = 'warning - customer already have an existing contract' then 1 else 0 end ) already_existing_contract_user,
	
	MAX(case when action = 'checkoutStepSubmitted' AND identifiers_page_name = 'checkout/appointment' then 1 else 0 end) AS appointment_submitted
FROM 
	base 
GROUP BY 1
)

/*
SELECT 'Appointment Page' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where appointment_page = 1
UNION ALL 
SELECT 'Click on Date Picker Icon' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where date_picker_click = 1
UNION ALL 
SELECT 'Date Picker View' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where date_picker_view = 1
UNION ALL 
SELECT 'Date Selected' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where date_selected = 1
UNION ALL
SELECT 'Time Slot Radiobutton' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where time_slot = 1
UNION ALL 
SELECT 'Appointment Submitted' AS "Screen", count(identifiers_sessionid) AS "Sessions" FROM base
where appointment_submitted = 1
*/





Select already_existing_contract_user 
,sum(appointment_page) total_appointment_landed
, sum(any_interaction_done) any_interaction_done
, sum(appointment_submitted) appointment_submitted
from intd
where appointment_page = 1 
group by 1
;

-- , limited_session as (
-- 	Select *
-- 	from base bs
-- 	where bs.appointment_submitted = 0  
-- 	and any_interaction_done = 0
-- 	limit 3
-- )

-- select bs.any_interaction_done , ev.identifiers_sessionid , ev.identifiers_page_name , ev.service_line ,ev.category , ev.ACTION , ev.label

-- from eshop_data.es_events_v2 ev
-- inner join limited_session bs on ev.identifiers_sessionid = bs.identifiers_sessionid
-- where bs.appointment_submitted = 0 
-- order by ev.identifiers_sessionid asc, identifiers_log_time asc


-- ;

```

## Sample Sessions

-  "c1d2fff91a25662d96115b1ed6a100a9ce231b24d648e5196e43e0f65b66684778ca0470877cc08506b2b75e22d74c503b1f3caec1fbf8559016c23a4a35d79e"

	o   User came to appointment page in the acquisition journey – got a prompt of an existing contract so couldn’t move ahead

	o   Need to simulate the same situation on prod using uat creds..

	o   Need to size this error

-   954e3f710f11677b97a70a2b47df1fd7a854028a0ee6f3bc34d188cf200929444bf0022b85bfd12ab9731dd07a8d6106e51ca5f341e3a0784eb58b94e742d598

o   Existing user got the pop up and then moved to change plan – landed on acv page

## Xyz

